# Changelog

All notable changes to MEHO are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This is the **project-wide** changelog. It covers all three
operator-facing artefacts under one document:

- the **backplane container image** at `ghcr.io/evoila/meho`,
- the **Helm chart** at `oci://ghcr.io/evoila/meho-chart`, and
- the **operator CLI** released as multi-platform tarballs at
  <https://github.com/evoila/meho/releases>.

There is no separate `cli/CHANGELOG.md` — this file supersedes that
scaffolding. The release-notes-extraction tooling in
`.github/workflows/cli-release.yml` reads from this file, and chart /
image releases reference the same `[Unreleased]` section until a tag
cuts the next version.

This top-level CHANGELOG is the **authoritative source** for the
GitHub Release notes published at
<https://github.com/evoila/meho/releases>. The
`.github/workflows/cli-release.yml` workflow extracts the section
matching the current tag (with `[Unreleased]` as fallback for
pre-release tags) and passes it to GoReleaser via
`--release-notes`, overriding GoReleaser's built-in git-log
generation. Operators see the human-curated narrative — what
shipped and why it matters — not a dump of commit subjects.

## How entries are added

- **One bullet per merged PR** under the appropriate category.
- Bullets land in `## [Unreleased]` until a tag cuts the release;
  the release-cutting PR moves them under the new `## [x.y.z] -
  YYYY-MM-DD` heading.
- **Each bullet links to the planning issue (and the PR once merged):**
  `- Add Vault probe (#30 / #47)` when both are known, or
  `- Add Vault probe (#30)` if the PR has not merged yet. The issue
  number is the planning anchor (`evoila-bosnia/meho-internal`); the
  PR number is the implementation (`evoila/meho`).
- **Conventional-Commits prefixes are optional in the bullet** —
  the category heading is doing the typing already. Keep the prose
  imperative and operator-readable.
- **Categories** (Keep a Changelog):
  - **Added** — new features.
  - **Changed** — changes to existing functionality.
  - **Deprecated** — soon-to-be removed features.
  - **Removed** — features removed in this release.
  - **Fixed** — bug fixes.
  - **Security** — vulnerability fixes; flag CVE / advisory.
  - **Breaking changes** — schema renames, body-shape changes, removed
    endpoints, or any other contract change that requires adopters to
    update their client code. Each bullet includes a migration recipe
    (the smallest concrete edit a v(N-1) client makes to keep working
    on v(N)). Surfaces above `Added` in the release section so
    adopters reading top-to-bottom see migrations before features.

**Connector release-notes convention.** Distinguish three connector
ship states; release-notes / kb / Goal-tracker text must say which
state the release ships, not the next state up. Full rubric in
[`docs/codebase/connector-release-readiness.md`](docs/codebase/connector-release-readiness.md).

- **Dispatch + catalog landed.** Connector class registered, ops
  register into `endpoint_descriptor`, `search_operations` indexes
  them, per-op `description` / `safety_level` / `requires_approval`
  metadata is curated, integration tests with **injected loaders**
  pass. Production execution against real per-target Vault
  credentials does NOT yet work. Language: *"Kubernetes typed
  connector dispatch + catalog (13 ops indexed; loader wiring tracked
  under #214)."*
- **Loader wired (single auth model).** As above, plus the default
  loader reads real operator-context per-target Vault credentials for
  one `auth_model`. Production dispatch executes end-to-end for
  targets with that auth_model. Language: *"Kubernetes typed
  connector — `service_account` auth model live; `per_user` auth
  model tracked under #N."*
- **Ops curated for production.** All advertised auth_models live;
  per-op descriptions + safety annotations make the op
  LLM-discoverable; onboarding doc validates against a real deploy.
  Language: *"vault-1.x typed op surface ready for production
  (`jwt-federated` auth model, full ops catalog)."*

The k3d / testcontainers / mock-loader integration test does not
promote a connector across these states. Promotion is per-auth-model
and requires the loader to read real Vault per real-target
credentials. Mention the live auth-model set explicitly on every
connector-related release-notes line.

## [Unreleased]

### Fixed — leaky `_ingest_getaddrinfo` test shim now delegates to the real resolver, killing the xdist cross-test flake (#2385)

- The ingest SSRF-guard mocks in `test_api_v1_connectors_ingest.py`
  (`_ingest_getaddrinfo` and the `_resolver_allowing` twin) patched the
  **global** `socket.getaddrinfo` attribute (`openapi.socket` is the
  shared `socket` module), so the patch leaked process-wide onto any
  worker also running the embedding/DNS path under `pytest-xdist`
  (`test_real_descriptor_embedding_path`, `test_g51_memory_canary`).
  The two-positional-arg signature crashed the leaked positional call
  with `TypeError: takes 2 positional arguments but N were given`
  (evoila/meho#574), and its early-stop masked a real failure at #2338's
  merge (hotfix #2383).
- Both stand-ins now accept the full `getaddrinfo` positional arity
  (`*args`/`**kwargs`) and delegate non-test hosts to the **real**
  resolver captured at import time (`_REAL_GETADDRINFO`) instead of the
  re-looked-up, still-patched `socket.getaddrinfo` — which would recurse
  infinitely. A leaked/active patch is now a transparent no-op, never a
  `TypeError` or `RecursionError`. SSRF-guard behavior is unchanged: test
  hostnames still resolve to the fixed public test IP and the destination
  guard still fires.

### Docs — curated-until-populator-covers policy for auto-only edge kinds + grandfather rule (#2336)

- Documented that the four auto-discoverable topology edge kinds
  (`runs-on`, `mounts`, `routes-through`, `belongs-to`) MAY be curated
  on any pair no populator covers — v0.2 auto-discovery is
  Kubernetes-only (base `discover_topology` is a no-op; only the k8s
  connector overrides it), so on a non-k8s pair no probe emits the edge
  and the curated write inserts clean (`source: curated, conflicts:
  []`). §6 conflict detection stays dormant because it only keys off
  `source='auto'` rows / existing different-kind edges.
- Recorded the **grandfather commitment**: any populator that begins
  covering a previously-uncovered kind ships with a one-shot
  reconciliation that keeps pre-existing curated edges visible and
  free of retroactive §6 conflicts — the same operator-owned-rows
  principle `refresh._refresh_curated_edge` already applies for the
  identical pair. Policy-only; the `curated_until_populator_covers`
  bucket and the reconciliation job are deferred to the non-k8s
  populator initiative. Updated `docs/architecture/topology.md` and
  `docs/cross-repo/topology-annotation.md`.

### Changed — `/ui/agents` polish: card toggle, no auto-open Stop dialog, sub-view back-nav (#2347)

- The `/ui/agents` cards now carry a one-click enable/disable **Toggle**
  (tenant_admin only), routed through the same `/ui/agents/{name}/toggle`
  endpoint the detail view uses — an operator no longer has to open the
  detail page and navigate back just to flip an agent's state.
- Submitting **Run** on an agent no longer auto-opens the "Stop this run"
  confirm dialog. The dialog ships inside the run-transcript fragment
  swapped in on submit, and the app-shell modal controller's
  `htmx:afterSwap` sweep was popping it the instant a run started, hiding
  the live logs. Button-driven dialogs now opt out of the sweep via
  `data-auto-open="false"`, so the Stop confirm opens only on the Stop
  button.
- The `/ui/agents/runs` list and per-run detail views gained a
  breadcrumb back to `/ui/agents` (the run detail now links the agents
  console directly, not only the runs list), matching the grants and
  principals sub-views.
### Fixed — disinherited leaking `hx-disabled-elt` find-selectors across `/ui` modals (#2340)

- Five operator-console modals (`scheduler` create-trigger, `memory`
  create + body-edit, `conventions` create + edit) bound
  `hx-disabled-elt="find button[type=submit]"` on the `<form>` to disable
  the submit button while a request is in flight. Because `hx-disabled-elt`
  is an *inherited* htmx attribute, each form's descendant htmx requests —
  the debounced cron-validate / token-preview `POST`s and the in-form
  Cancel `hx-get` — inherited the value and resolved `find button[type=submit]`
  against their own (submit-button-less) subtree, logging
  `The selector "find button[type=submit]" on hx-disabled-elt returned no
  matches!` on every such request while the intended disable-submit
  affordance silently never fired. Each form now carries
  `hx-disinherit="hx-disabled-elt"` (the pattern established for the runbook
  editor in #2174), scoping the value to the form's own submit. A generic
  `test_ui_templates.py` guard enforces the discipline across every `/ui`
  template so a new modal cannot reintroduce the leak.

### Fixed — unified the `/ui` CSRF double-submit token pattern (#2345)

- The operator console's `/ui/*` write surfaces no longer `403
  csrf_token_invalid` on the 3rd repeated write of a session. The
  codebase mixed two CSRF patterns — some renders rotated the
  `meho_csrf` cookie on every fragment render while others reused the
  live cookie — so an open modal's echoed token could drift out of sync
  with the cookie. The double-submit token is now **session-stable**:
  `mint_csrf_token` derives it deterministically from the session, so
  re-minting on any render returns the identical value and the token
  only changes when the session rotates at an auth boundary. A global
  `htmx:configRequest` hook (`static/src/app/csrf-token.js`)
  additionally re-reads the live `meho_csrf` cookie at request time and
  overrides the `X-CSRF-Token` header, so the double-submit pair can
  never drift regardless of render order. Security is unchanged (the
  HMAC binding to the session id still defeats cookie injection).
### Fixed — `/ui/agents/create` now shows backend validation errors (#2346)

- The Create-agent modal previously swallowed backend 4xx validation
  responses: the handler re-rendered the modal with per-field errors but
  returned it as `409` / `422`, and HTMX's default response handling
  does not swap a non-2xx fragment — so a bad `identity_ref`, a
  duplicate name, or an out-of-range `turn_budget` made the Create
  button appear to "do nothing" while the failure was only visible in
  DevTools. The recoverable-error re-render now returns `200` (the same
  inline-error mold the runbooks start-run and conventions author modals
  already ship) and carries a top-of-form error banner in addition to
  the existing per-field messages, so the operator sees exactly which
  field to fix and keeps their typed input. Sibling `/ui` create/edit
  forms with the same missing handler (memory, kb) are tracked
  separately for a follow-up sweep.
### Fixed — scheduler one_off `fire_at` picker now converts local time to UTC on submit (#2339)

- The `/ui/scheduler` Create-trigger modal's `fire_at` `datetime-local`
  picker showed the operator their own wall-clock time but posted a
  naive string the engine stored verbatim as UTC, so a CEST (UTC+2)
  operator who picked "11:00" got a trigger that fired at 11:00Z — two
  hours late. The modal now converts the picked local value to a
  UTC ISO-8601 instant on submit (via the browser's DST-aware `Date`,
  so CET/CEST both resolve correctly), and a live hint next to the
  field shows the resolved UTC so the conversion is visible rather than
  silent. Engine-side `fire_at` handling is unchanged.

### Breaking changes — GET-list endpoints converged on the `{items, next_cursor}` envelope (#2338)

- **BREAKING.** The seven reference `GET`-list endpoints now return the
  unified `{items, next_cursor?, ...sidecars}` list envelope
  (`docs/codebase/api-shape-conventions.md` §2) as their default **and
  only** shape. The `?envelope=v2` opt-in that bridged the migration
  (G0.16-T6 #1312 / G0.18-T3 #1356 / G0.22-T6 #1611) was retired. A
  platform-wide contract test now pins the convention so a new list
  endpoint can't reintroduce a divergent shape. Affected endpoints and
  the wire-shape change adopters must migrate:
  - `GET /api/v1/targets` — was a bare `[TargetSummary, …]` array → now
    `{"items": [...], "next_cursor": <name|null>}` (keyset-paginated).
  - `GET /api/v1/connectors` — was `{"connectors": [...]}` → now
    `{"items": [...], "next_cursor": null}`.
  - `GET /api/v1/conventions` — was `{"entries": [...], "budget_status":
    {...}}` → now `{"items": [...], "next_cursor": null, "budget_status":
    {...}}` (sidecar unchanged).
  - `GET /api/v1/audit/my-recent` — was `{"rows": [...], "next_cursor":
    …}` → now `{"items": [...], "next_cursor": …}`.
  - `GET /api/v1/broadcast/overrides` — was a bare
    `[BroadcastOverrideRead, …]` array → now `{"items": [...],
    "next_cursor": null}`.
  - `GET /api/v1/runbooks/templates` — was `{"templates": [...]}` → now
    `{"items": [...], "next_cursor": null}`.
  - `GET /api/v1/runbooks/runs` — was `{"runs": [...]}` → now
    `{"items": [...], "next_cursor": null}`.

  **Migration recipe.** Read the list from `response["items"]` instead
  of the old key (`connectors` / `entries` / `rows` / `templates` /
  `runs`) or the bare array; read `response["next_cursor"]` for
  pagination (only `targets` populates it today). The bundled `meho`
  CLI and the generated Go client already read the new envelope — this
  change ships them together. Clients still sending `?envelope=v2` are
  unaffected (the now-unknown param is ignored). The `audit` sibling
  reads (`/query` / `/who-touched` / `/by-work-ref`) and the topology
  closure reads keep their existing shapes.

### Changed — structured `connector_auth_failed` at session establish across the VCF family (#2329)

- A session-establish login rejected by a rotated/stale/locked-out
  credential (a `401`/`403` on `POST /api/session`, `/api/session/create`,
  the VCF Automation provider/tenant logins, SDDC Manager `/v1/tokens`, or
  vRLI `/api/v2/sessions`) now returns the structured
  `connector_auth_failed` result instead of a bare
  `connector_error: RuntimeError` that buried the auth cause in an
  interpolated exception string. The envelope carries a `cause` sub-code
  (`session_establish_401` / `_403`), the `target`, its `secret_ref`, a
  `remediation` naming the `meho target credential set <name>` restage
  command, and the original message in `raw_message` — the #2091
  `connector_vault_forbidden` mold. The family connectors now raise a
  shared `ConnectorAuthError` (a `SessionLoginError`/`RuntimeError`
  subclass, so existing callers are unaffected) that the dispatcher
  recognises ahead of its generic error arm and maps to the **same**
  builder the mid-session-401 recovery path (#2067) reaches — so the code
  is emitted consistently regardless of *when* auth fails, including when
  the one-shot mid-session re-establish is itself rejected. A consumer can
  now switch on `error_code` to distinguish "restage the stale credential"
  from a generic connector crash (#2329).

### Added — CI trip-wire: every packaged catalog entry dry-run-resolves without a bare-400, plus a raw-spec-URL upstream audit (#2334)

- A packaged connector-catalog entry whose `upstream` pointed at an HTML
  developer-portal page used to fetch the HTML, YAML-decode it, and surface
  an opaque bare-400 (`could not decode spec … line 33`) that told the
  operator nothing actionable. The structured `catalog_entry_upstream_not_spec`
  content-type guard (G0.15-T2 #1211) already closes that at request time;
  #2334 adds the CI guardrails that keep the contract from regressing: a
  parametrized fixture dry-run-ingests **every** shipped catalog entry (with
  all fetchable upstreams mocked to serve worst-case HTML — deterministic, no
  live network) and asserts each resolves to a dry-run success or a
  **structured** error envelope, never the opaque decode-400 or an unhandled
  500. A companion static audit asserts every fetch-path `upstream` is a raw
  OpenAPI URL (`.yaml`/`.yml`/`.json`), not a documentation portal —
  generalizing the prior Broadcom-only sweep. A future row (or URL edit) that
  drifts into the portal/HTML pattern now trips at unit-test time instead of
  on an operator's first POST. Audit finding: the shipped catalog is already
  clean — only `harbor` and `gh` reach the fetch path and both point at
  raw-content URLs (#2334).
### Added — inline spec content on the MCP `connector.ingest` tool (#2326)

- Each `specs[*]` entry on the `meho.connector.ingest` MCP tool now
  accepts an optional `content` field carrying the spec text inline
  (~20 MiB cap, the REST bound). When set, the backplane uses the bytes
  verbatim and skips the fetch — the same `SpecSource.content` on-ramp
  the CLI upload already uses. Previously the tool required a `uri`-only
  entry and rejected `file://` / `docs:` schemes at the https fetch
  guard, forcing agent-driven flows to publish private lab specs to a
  public gist purely to satisfy the fetcher. Inline content bypasses the
  fetcher entirely (no SSRF surface, strictly safer than the gist
  workaround it retires), so an appliance-served or hand-authored spec
  with no public https URL (NSX, VCFA) can be ingested over a fully
  MCP-drivable flow; `uri` stays the audit label, so the resulting
  L1/L2 rows match a CLI upload of the same file (#2326).
### Tested — regression pin: a non-dry-run 1275-op-class spec ingest keeps the event loop responsive (#2333)

- Pinned the v0.8.0 large-spec ingest crash fix end-to-end: a
  **non-dry-run** ingest of the 1275-op class (the canonical 7.5 MB
  vmware/9.0 signal) now has a regression test proving the event loop
  stays responsive while every operation is committed. A heartbeat
  coroutine measures loop lag concurrently with
  `IngestionPipelineService.ingest(dry_run=False)`; the test asserts all
  1275 ops persist and the loop keeps ticking with no starvation. A
  deterministic companion injects a fixed blocking parse and proves the
  `asyncio.to_thread` offload keeps it off the loop, and a third pins
  that the `--catalog` (`catalog_entry`) shape resolves onto the same
  `202` + job-handle async path rather than the synchronous pass that
  crashed the pod. Test-only; no runtime change (the fix shipped in
  #2275 / #2317).

### Added — structured post-approval vault-write-forbidden error + write-capability warning on the approval envelope (#2331)

- A typed `vault.kv.put` / `patch` / `delete` that Vault denies at
  dispatch now surfaces a dedicated `connector_vault_write_forbidden`
  result (the write-side sibling of the read-oriented
  `connector_vault_forbidden`, #2091) naming the exact `<mount>/data/<path>`
  Vault denied, the acting operator identity, and the write-policy stanza
  to add — instead of a bare permission-denied or a misleading read-path
  diagnosis. The park-time `sys/capabilities-self` write preflight already
  shipped; #2331 promotes its `will_be_denied` signal to a top-level
  `write_capability_warning` on the parked `proposed_effect` envelope so
  the approval surface can render a "this write may not land" banner
  (a warning, not a gate), and documents the per-operator templated
  write-identity contract in `docs/cross-repo/connector-vault-policy.md`
  (#2331).
### Changed — reject `kind=event` scheduler triggers at create until #826 lands (#2325)

- Creating a `kind=event` scheduled trigger now returns a structured 422
  `event_triggers_not_implemented` (MCP invalid-params with the same
  code; UI modal banner naming #826) on **every** transport — REST, MCP,
  and the operator console. Previously the create succeeded and the
  trigger reported `status=active`, but it could never fire: the
  event-subscription matcher in `events/drain.py` is still the documented
  T5 no-op, so events that real producers already emit onto the outbox
  (e.g. `operations/agent_run.py` publishes agent-run terminal-transition
  events) were silently swallowed. Accept-and-never-fire is dishonest;
  the honest shape until #826 wires the matcher is a refusal at create.
  The guard is a single check in `SchedulerAdminService.create` — removed
  in the same change that lands #826.
- Any pre-existing `active` event trigger is parked to `paused` by a
  one-shot startup reconcile (`reconcile_active_event_triggers`, run once
  before the first scheduler tick), with the reason logged under
  `scheduler_event_triggers_parked` so an operator sees it parked (via
  `?kind=event&status=paused` or the UI list) rather than misleadingly
  active. No schema change, no migration; `cron` / `one_off` creation is
  unchanged.
### Fixed — scheduler Vault token renews on use + startup/periodic lookup-self (defuses the ~32-day periodic-token fuse) (#2328)

- The scheduler now fires a best-effort `auth/token/renew-self` after
  every successful Vault agent-secret read/write, so the documented
  **periodic** service token (`-period=768h`) is renewed at scheduler-tick
  frequency and never ages out while the process runs. Previously the
  backplane never renewed it — a token minted per the onboarding guide
  silently died ~32 days after standup and every Vault-first credential
  read started returning 403. Renewal failures are logged and swallowed
  (the read/write already succeeded), never a new failure mode.
- The scheduler token is now self-looked-up (`auth/token/lookup-self`) at
  startup and on a slow cadence (hourly), logging a dead or unreachable
  token as a loud `scheduler_vault_token_dead` /
  `scheduler_vault_token_unreachable` ERROR the moment it's observed —
  cutting time-to-notice from weeks to minutes. The healthy path logs
  `scheduler_vault_token_verified` with the token's `ttl`/`expire_time`
  so operators can watch expiry advance across renewals.
- The token is now resolved from its live source on every use. A new
  optional `VAULT_SCHEDULER_TOKEN_FILE` names a file (a Vault-Agent
  sidecar sink) the token is re-read from per read/write, so a re-mint is
  picked up **without a pod restart** — removing the manual re-mint +
  Secret-patch + pod-restart remediation the fuse otherwise forced. The
  static `VAULT_SCHEDULER_TOKEN` remains the default; an unreadable/empty
  file falls through to it.

### Added — scheduler skip-state on the trigger row + park after N unresolvable skips (#2327)

- The scheduler tick loop now projects its precondition **skips** onto the
  `scheduled_trigger` row (migration `0057`, three columns): `skip_count`
  (consecutive skips since the last successful fire), `last_skip_reason`
  (machine tag — `definition_missing` / `definition_disabled` /
  `credentials_unresolved`; the corrupt-cron / unknown-kind park paths also
  stamp `invalid_cron_expr` / `unknown_kind`), and `last_skipped_at`.
  Previously a permanent misconfiguration (revoked scheduler Vault token,
  deleted-but-referenced definition, never-persisted agent secret) made the
  loop skip every 30 s tick with the only trace a pod-log WARN, while
  `meho scheduler list` showed a healthy-looking `active` trigger — a real
  deploy lost ~360 hourly fires over 15 days before anyone noticed.
- After a fixed number of **consecutive** unresolvable skips the loop parks
  the trigger (`status='paused'`) so the state machine itself communicates
  "broken, stopped trying" instead of re-tripping forever; a successful
  fire resets the streak and clears the skip fields. The new state is
  surfaced on `GET /api/v1/scheduler/triggers`, `meho.scheduler.list` /
  `.show` (MCP), `meho scheduler list` (a `SKIPS` column), and the operator
  console (a warning badge on the list row + a skip block on the detail
  page). The at-most-once fire contract and transient-retry behaviour are
  unchanged — the columns are additive visibility.

### Added — redaction-safe `proposed_effect` preview for `vault.kv.*` credential writes (#2332)

- A parked `vault.kv.put` / `patch` / `delete` approval request now
  carries a bespoke, redaction-safe `proposed_effect` preview instead of
  the op-identity-only default. The approver sees the KV **mount**,
  **path**, KV **version**, the write **semantics** (`put` = wholesale
  replace, `patch` = merge, `delete` = version soft-delete), and the set
  of **key names** being written — never their **values**. This restores
  the approver's ability to distinguish "rotate the throwaway probe key"
  from "clobber the production database password" while keeping the
  value-redaction promise the credential-class suppression established
  (#1422 / #1856). Wired as a bespoke builder (`vault.kv.put` / `patch`
  classify `credential_write`, so the generic params-echo default is
  suppressed for them), mirroring the Keycloak user-create builder mold
  (#1857).
- Every parked-request envelope now carries two reviewer-facing
  provenance fields: `preview_populated` (a `bool` a caller can read to
  refuse to auto-approve a blind, op-identity-only request) and, when a
  preview is intentionally sparse, `preview_reason` —
  `credential_write_redacted` (a deliberately-redacted credential write
  with no bespoke builder) vs `connector_did_not_populate` (a
  non-credential op that simply never populated one) — so the approval
  surface can style the blind case as elevated-risk. `proposed_effect`
  is a free-form JSON field, so no API schema / OpenAPI change (#2332).
### Added — approval-TTL lifecycle wired end-to-end (parked approvals now expire) (#2322)

- Every parked approval is now stamped `expires_at = created_at +
  APPROVAL_DEFAULT_TTL` (new `APPROVAL_DEFAULT_TTL_SECONDS`, default 14
  days) at park time across **all** transports — REST, MCP, and run-bound
  dispatches (they all park through the dispatcher's
  `_handle_needs_approval`, plus the composite park path). An explicit
  caller deadline is honoured but capped at that ceiling, so no surface
  can park an unbounded-lived approval. Previously every parked row
  carried `expires_at: null` and the `expired` status / column /
  `expire_stale_requests` helper existed but had zero production callers —
  the TTL lifecycle was shipped but inert.
- A dedicated periodic sweeper (`operations.approval_expiry`, gated on
  `APPROVAL_EXPIRY_ENABLED`, `APPROVAL_EXPIRY_TICK_INTERVAL_SECONDS`
  default 300s) drives `expire_stale_requests` per tenant under a
  `system:approval-expiry` operator, so past-deadline pending approvals
  transition to `expired` within one tick, each with a decision audit row
  and a fail-open `approval.expired` broadcast. Expired rows leave the
  default `?status=pending` view and cannot be approved or resumed.
- Legacy pre-#2322 rows with `expires_at IS NULL` age out too: the sweep
  coalesces a null deadline against `created_at + APPROVAL_DEFAULT_TTL`
  (no schema change, no migration) and backfills the deadline on expiry.

### Fixed — agent-run resume stamps `parent_audit_id` so approved chains replay as one subtree (#2323)

- Completing #2086: an approval chain resumed by the **in-process agent
  runtime** (broadcast-driven `/decide` / MCP path) now emits its executed
  DISPATCH row with `parent_audit_id` pointing at the `approval.request`
  row, matching what the decision row already did. Previously the agent
  waiter re-dispatched on the agent's own task, where `parent_audit_id_var`
  still held the top-level value (`None`), so the executed row orphaned as a
  **second root** in `meho.audit.query` / `meho.audit.replay` even though
  `agent_session_id` anchored the chain.
- Fix is scoped to `agent/approval_wait.py::_resume_approved_or_already_resumed`:
  it loads the parked row's pre-generated `request_audit_id` (tenant-isolated
  `get_request`) and binds `parent_audit_id_var` (token-scoped) around
  `call_operation_with_approval`. The operator/REST resume path already bound
  it via #2312's `_dispatch_resume_with_bound_context`; this closes the
  remaining agent-run gap. No schema change (column shipped in migration
  0053), no route/OpenAPI change.
- Adds a regression test that drives the four-eyes agent decide-path resume
  end to end and asserts the executed dispatch back-links to the request,
  the chain has exactly one null-parent row, and `replay_session`
  reconstructs one subtree (`approval.request` → {`approval.decision`,
  `<op_id>`}).

### Fixed — vCenter filtered listings key `filter.*` params off the mount flavor (modern `/api` no longer 400s) (#2298)

- Filtered vCenter listings sent legacy `/rest`-style `filter.`-prefixed
  query params (`filter.datastores`, `filter.hosts`, `filter.types`, …) on
  **every** mount. Real vCenter 8.x serves the modern `/api` surface, which
  addresses those FilterSpec fields by their bare name and returns HTTP 400
  for the prefixed form — so `datastore.usage`'s VM-placement enrichment
  400'd on every row, `network.portgroup.audit` was structurally broken, and
  the typed `host.usage` / `host.network_uplinks` listings 400'd whenever a
  host filter was supplied. Unfiltered listings were unaffected (which is why
  the gap slipped past CI).
- One shared adaptation helper (`VmwareRestConnector.adapt_op_query`,
  delegating to `_mount.adapt_filter_params`) now keys the param style off
  the target's established mount at the transport seam: bare names on modern
  `/api`, `filter.`-prefixed on legacy `/rest` / vcsim. Applied once at the
  composite read + write sub-call seams and the two typed-op listing legs —
  not per call site. Adds a modern-mount mock that 400s the prefixed form so
  the vcsim-only test gap is closed; no new op_ids, no OpenAPI change (#2298).
- Also routes `vm.info`'s name→moid listing leg (which entered via the
  `origin/main` merge of #2354, predating the seam) through `adapt_op_query`,
  so its `filter.names` param is likewise keyed off the mount flavor instead
  of 400'ing on a modern `/api` vCenter 8.x; pinned with modern/legacy mount
  tests mirroring `host.usage` (#2298).
### Fixed — VMware-family CLI verbs repointed to typed op_ids (#2355)

- The 19 VMware-REST-family CLI verbs whose backend reads were converted to typed
  ops in #2266 now dispatch the **dotted** typed op_ids (`vrops.liveness`,
  `nsx.node.status`, `sddc.domain.list`, `fleet.about`, `vcfa.provider.org.list`,
  `vrli.event.query`, …) instead of the legacy `METHOD:/path` op_ids. On a fresh
  boot with zero catalog ingest — the state #2266 makes canonical — the legacy
  op_ids no longer resolve, so `meho vcf-operations|vcf-fleet|vcf-automation|
  sddc-manager|nsx|vcf-logs …` verbs were dead-ends; the repoint restores them.
- `meho vcf-logs query` now aligns with `vrli.event.query`'s closed
  `parameter_schema`: it sends `limit` as an integer (was a string) and no longer
  sends `timestamp_window` (the schema accepts only `constraints` + `limit`, so
  the retired param would trip `additionalProperties:false`). The `--time-range`
  flag is removed — compose any time-range constraint into the `constraints`
  expression, which the typed op renders into the request path.
- Unconverted verbs and the flagged method/semantic-mismatch verbs
  (`vcf-operations resource list/get`, `sddc-manager domain info`/`about`,
  `nsx node list`) keep their legacy op_ids until their typed ops land.

### Removed — retired the ingested-curation apparatus in the six VMware-family connectors (T7 · #2358)

- Deleted the hand-curated ingested-enable apparatus (`core_ops.py` /
  `vcf_automation/_core_data.py`) from all six VMware-family connectors —
  `sddc_manager`, `vcf_fleet`, `vcf_operations`, `vcf_logs`, `nsx`,
  `vcf_automation` — dropping ~6.5k lines of dead code with **zero production
  callers**: the `*_CORE_OPS` flip-lists, `*_CORE_GROUPS`, path classifiers
  (`classify_*_op`), and appliers (`apply_*_core_curation`) were only ever
  invoked by tests. The audited operational reads are already typed ops
  (T1–T6, #2295/#2302–#2306); the remaining 33 declined ops stay browsable as
  `source_kind="ingested"` breadth and are enabled through the generic review
  flow (`ReviewService.enable_reads` / MCP `meho.connector.enable_reads` / REST
  `POST /api/v1/connectors/{id}/enable-reads`). No REST/schema change — no
  OpenAPI snapshot delta. The `(product, version, impl_id, connector_id)`
  identity constants each package re-exports are preserved (relocated into the
  package `__init__.py`), so `from meho_backplane.connectors.<pkg> import
  <PROD>_CONNECTOR_ID` still resolves.
- **Operator action — vRLI canonical-spec swap on the consumer deploy.** If a
  deploy still serves the hand-edited **gist overlay** of the `vcf-logs-9.0`
  OpenAPI spec, retire it in this upgrade: the two blockers that forced the
  overlay are fixed as of v0.20.0 (`#2066` `{+path}` reserved-expansion render,
  `#1796` `servers[]` base path), so the **canonical** spec now ingests clean.
  Re-ingest the canonical spec, confirm it does not trip the `#2272`
  YAML-timestamp ingest class, spot-check a read (`GET:/api/v2/version` → ok;
  the typed `vrli.event.query` op is unaffected — typed reads have no ingest
  dependency), then delete the gist. No MEHO code change is required — it is an
  operator action on the deploy. Tracked with a runbook on `#2358` and
  coordinated on `evoila-bosnia/claude-rdc-hetzner-dc#1790`.

### Changed — nsx/connector.py split under the file-size budget (#2356)

- `nsx/connector.py` dropped from 600 to ~562 lines, back under the
  `code-quality.py` file-size ceiling (block limit 600), with zero behaviour
  change. The pure module-level `_is_acceptable_auth_model` predicate moved to
  `nsx/session.py` as the public `is_acceptable_auth_model` (imported back at the
  `auth_headers` boundary), and the module docstring was trimmed to the house
  norm. The seven typed-read shims (`node_status`…`alarm_list`) stay bound
  methods on `NsxConnector` with their exact names so the dispatcher's
  `module.ClassName.method` handler resolution (`operations/_handler_resolve`)
  is unchanged; typed reads still register and dispatch `source_kind="typed"`.

### Added — vm.power single-VM gated write verbs (incl. guest-shutdown)

- New `vmware.composite.vm.power` write composite acts on **one** VM for
  one-off incident actions (a hung appliance) — the single-VM ergonomics the
  fan-out `vm.power.bulk` is clumsy for. Five verbs: `on` / `off` / `reset`
  hit the hard `POST:/vcenter/vm/{vm}/power` endpoint; `guest_shutdown` /
  `guest_reboot` hit the Tools-mediated `POST:/vcenter/vm/{vm}/guest/power`
  for a clean in-guest transition. A soft verb against a VM whose VMware
  Tools are not running fails **typed** (`status="tools_unavailable"`,
  echoing the Tools state) instead of hanging, so the operator can fall back
  to a hard `off`. Approval-gated per row (`dangerous` +
  `requires_approval=True`) on the shipped write mold (#2256/#2254); the
  park-time preview echoes the VM, verb, and a `power_kind` (`hard` vs
  Tools-soft `guest`) so the approver sees the soft-vs-hard blast radius.
  `vm.power.bulk` is unchanged. (#2301)

### Added — typed vSphere incident reads (vm.info/object.collect/tasks.recent)

- **Three new `source_kind="typed"` vmware-rest reads for incident
  survival** (#2300), each a bound method on `VmwareRestConnector` that
  reads directly on the connector session via PropertyCollector — no
  `dispatch_child`, no ingested descriptor, so they dispatch on a fresh
  boot with zero catalog ingest:
  - **`vmware.vm.info`** — one VM's live triage signals (power state,
    guest IP, VMware Tools status/running, guest heartbeat, per-datastore
    usage) addressed by moid or name. The "poweredOn but no guest IP"
    hung-appliance shape is representable in one call — data the plain
    vCenter REST VM detail (configuration only) cannot supply.
  - **`vmware.object.collect`** — a bounded generic PropertyCollector
    read of a caller-specified property set off one `(type, moid)`
    object. Bounded by construction (one object, no traversal, ≤64 paths
    each ≤16 segments deep); oversized/malformed requests return a
    structured `invalid_params` error before any read is issued.
  - **`vmware.tasks.recent`** — recent vCenter Task objects (operation,
    entity, state, progress, queued/started/completed times) for
    change-window monitoring, distinct from `event.tail`'s event-log read.

### Connectors — SDDC typed reads (12-read lab-audit set incl. credential_read-gated /v1/credentials) (#2306)

- The audited 12-read SDDC Manager lab-audit set — `domains`, `domains/{id}/status`,
  `clusters`, `hosts`, `vcenters`, `nsxt-clusters`, `credentials`, `tasks`,
  `system`, `vcf-services`, `sddc-managers`, `license-keys` — is now first-class
  **typed** ops (`source_kind="typed"`, `sddc.*` op-ids) that dispatch on a fresh
  boot with zero catalog ingest, on the post-#2290 `session_login_token` token
  session, with one-shot 401 recovery via the #2067 `invalidate_session` seam.
- `sddc.credential.list` (`GET /v1/credentials`) is gated as a credential-read:
  `requires_approval=True` routes it through the policy-gate approval queue (not
  dispatchable without operator approval), its op-id classifies as
  `credential_read` so audit/broadcast rows collapse to aggregate-only, and the
  handler scrubs every secret-keyed value at the connector boundary — no
  credential material rides the result.
- `core_ops.py` ingested-row curation is repointed for the converted ops: the 5
  overlapping reads leave the curated set (now 4 non-audited browse-breadth reads:
  release, domain detail, network-pools, bundles); the ingested VCF catalog stays
  browsable as profiled-dispatch breadth under its own `METHOD:path` op_ids — two
  surfaces, no resolver shadowing (#2262 invariant holds).

### Connectors — vRLI events query converted to a typed op on the connector session (#2295)

- The vRLI (VCF Operations for Logs) events query is now a
  `source_kind="typed"` op — `vrli.event.query`, a bound method on
  `VcfLogsConnector` that issues `GET /api/v2/events/<constraints>` (with an
  optional `limit`) directly on the connector's authenticated session and
  recovers a 440/401 session expiry with one re-login + retry (the #1909/#1135
  soak scenario). It works on a fresh boot with **zero catalog ingest**, so
  the op no longer depends on per-deploy `endpoint_descriptor` state (the
  #2247 failure class Initiative #2266 retires for the VCF family). The
  reserved-expansion constraint sub-path (`text/CONTAINS error/...`) is
  rendered with literal slashes so it reaches the appliance intact
  (#2003/#2066). `vcf_logs/core_ops.py` no longer flips an ingested row for
  the events query; the other six curated ops stay ingested (declined from
  typed conversion — unused in the adopter's real operations, and the
  ingested canonical spec covers the browse case). First conversion in
  Initiative #2266; the hand-edited production-overlay retirement is a
  follow-up RDC-team coordination step. (#2295)

### Added — vROps typed reads (liveness, alerts, resource query) (#2303)

- The vROps (`vcf-operations`) connector's audited read set now dispatches
  as **typed** ops (`source_kind="typed"`) directly on its hand-rolled HTTP
  Basic (+ optional `auth-source`) session, so they work on a fresh boot
  with **zero catalog ingest** (no per-deploy spec ingestion or operator
  review): `vrops.liveness` (`GET /suite-api/api/versions/current` — the
  documented reachability surface; the adopter's `casa/health` names a
  private/undocumented CaSA API), `vrops.alert.list`
  (`GET /suite-api/api/alerts` alert triage), and `vrops.resource.query`
  (a body-shaped `POST /suite-api/api/resources/query` with a typed
  `ResourceQuerySpec` request body). All three are read-only, `safe`, no
  approval. The two converted GET reads are removed from the `core_ops.py`
  ingested curation so the ingested twin is never flipped alongside the
  typed op (the #2262 no-shadow invariant); the remaining 6 ingested-browse
  ops stay curated as breadth until the Initiative #2266 T7 apparatus
  retirement. Unconverted curated ops (resource list/get, alert
  definitions, symptoms, recommendations, super metrics) are declined from
  typed conversion — not in the adopter's audited operational set. (#2303)

### Added — NSX typed reads on the cookie+XSRF session (audited set) (#2302)

- The audited NSX operational read set is now first-class **typed** ops
  (`source_kind="typed"`) that dispatch on a fresh boot with **zero
  catalog ingest** (no per-deploy curation state, the #2247 failure
  class): `nsx.node.status` + `nsx.cluster.status` (manager/cluster
  status+version), `nsx.backup.config` + `nsx.backup.status`,
  `nsx.transport_zone.list`, `nsx.tier1.list`, and `nsx.alarm.list`
  (optional status/feature/severity filters). `nsx.backup.config` is
  first-class for the backup disk-fill incident class (Broadcom KB 442696
  shape): it surfaces `backup_enabled`, `passphrase_configured`, and the
  retention-relevant `backup_schedule` / `remote_file_server` fields, and
  scrubs the backup passphrase + any nested SFTP credential at the
  connector boundary (the default redaction policy masks
  `password`/`secret` but not `passphrase`). These reads recover from
  session expiry through the #2067 dispatcher seam — `NsxConnector` now
  exposes the public `invalidate_session` hook, so a 401 evicts the cached
  cookie+XSRF session and re-dispatches once, no restart. The remaining
  reads (transport-node listing, segments, tier-0 gateways,
  distributed-firewall policies + rules) stay as ingested-row curation in
  `core_ops.py` so the wider ingested breadth is still browsable; the
  converted ops are no longer flipped by `apply_nsx_core_curation`. `tier-1
  gateway create` (a write) is out of scope. (#2302)

### Connectors — VCFA typed reads on the dual-plane session (#2305)

- The audited VCF Automation read set — provider org list / region list /
  health (`/cloudapi/1.0.0/site`) and tenant `/iaas/api/projects` + `about`
  — is now served by first-class `source_kind="typed"` operations
  (`vcfa.provider.org.list` / `region.list` / `health`,
  `vcfa.tenant.project.list` / `about`) that dispatch through the
  connector's own dual-plane session with **zero catalog state**. VCFA
  ships no vendor OpenAPI spec, so the prior ingested-curation path was
  dispatch-inert on a real deploy; typed conversion is the only working
  read surface. Each op declares the auth plane it rides (provider vs
  tenant), cross-checked against the request path at import time. The
  remaining `core_ops` ingested-curation surface is trimmed to the 6-op
  browse remainder; `org create` (a write) stays out of scope (#2305).

### Changed — Fleet typed reads on the LCM-local Basic session (#2304)

- VCF Fleet's audited read set — the **about/health probe** (`fleet.about`)
  and the **component inventory** ("what's deployed", `fleet.environment.list`)
  — now dispatches as `source_kind="typed"` off the connector's existing
  HTTP Basic (LCM-local) session, registered in code at lifespan startup.
  These ops work on a fresh boot with **zero catalog ingest**, removing the
  Fleet operational surface's dependence on ingesting the crash-prone
  vRSLCM-derived Fleet OpenAPI spec (the #2272 datetime-crash artifact). The
  remaining six curated ops (datacenter/vcenter list, environment detail,
  product list, request list/detail) are declined from typed conversion —
  outside the adopter's audited set — and stay as browsable ingested breadth
  until the curation apparatus is retired (T7); their ingested `/about` and
  `/environments` duplicates no longer curate, so they cannot shadow the
  typed ops. (#2304)

### Added — structural OpenAPI 3.x metaschema gate on spec ingest (#2292)

- Spec ingest now validates a decoded OpenAPI 3.0/3.1 document against the
  official OpenAPI metaschema (via `openapi-spec-validator`), immediately
  after the existing version check. A metaschema-invalid document — `paths`
  as a list, an operation with a non-object `responses`, a missing required
  field — is refused with a structured `invalid_spec` error naming the
  failing JSON path (e.g. `$.paths['/pets'].get.responses`) and a
  remediation line, on every transport (REST 400, MCP `-32602`, async-job
  `error`). Because the gate lives at parse, `dry_run` and the real ingest
  refuse an invalid spec identically, so a structurally broken document can
  no longer partially ingest into catalog rows of unknown quality.
  Validation is metaschema-only (no `operationId`/parameter semantic
  add-ons) so legal vendor specs — and every shipped package-data spec, at
  boot — keep ingesting; Swagger 2.0 still gets its dedicated conversion
  remedy, and the parser's tolerant-skip of sub-document junk is unchanged.
  (#2292)

### Added — persisted spec provenance at ingest (sha256 + origin + operator/timestamp, surfaced in review) (#2291)

- Every accepted spec ingest now writes a durable, non-spoofable
  provenance row to a new `spec_provenance` table (Alembic `0056`): the
  `sha256` over the **raw spec bytes** (hashed at the fetch/upload trust
  boundary before any decode), the audit `uri` as presented, the
  `origin` (`fetched` https GET · `inline` operator upload · `shipped`
  MEHO-authored catalog data), the ingesting operator, and a timestamp,
  scoped like the descriptor rows (`tenant_id IS NULL` = global). Before
  this, the only provenance was a spoofable `spec:<uri>` tag — an
  operator's hand-mutated inline upload labelled with a vendor's https
  URL persisted identically to a genuine fetch of that URL, and the
  fetched-vs-inline bit was never persisted. Re-ingesting the same spec
  updates the row in place (new `sha256` + timestamp), so different
  content under the same label is detectable. Provenance is surfaced on
  the connector review REST payload (`ConnectorReviewPayload.provenance`)
  and rendered by `meho connector review`; connectors ingested before
  this landed read as "unknown (pre-provenance)". Record-and-surface
  only — no refusal policy, and the `file:///` / `docs:` inline on-ramp
  stays fully functional. (#2270 / #2291)

### Changed — SDDC Manager auth rebuilt on the `session_login_token` profile scheme (#2290)

- Flip the shipped `sddc_manager_minimal.yaml` profile from the false
  `basic` scheme to `session_login_token`, and rebuild `SddcManagerConnector`
  auth atop it: the connector now establishes a session at
  `POST /v1/tokens` (JSON `{username, password}` → `accessToken`) and sends
  `Authorization: Bearer <accessToken>` on every request — SDDC Manager
  rejects HTTP Basic outright (live 401; Broadcom KBs 435716/387124/372387),
  so the ingested sddc catalog was never dispatchable before. Session
  mechanics are derived from the shared `session_login_token` scheme spec
  (single source with a profile-stamped connector), with per-`(tenant_id,
  target.id)` token isolation, single-flight establish, and one-shot 401
  recovery through the #2067 dispatcher seam (`invalidate_session` hook) —
  no restart. The hand-rolled class stays the resolution winner, preserving
  the #1750/#1798 product-shadowing invariant. Removed the false
  `username@sso_realm` Basic decoration. (#2271 / #2290)

### Added — operator-selectable auth scheme on non-catalog ingest (#2289)

- `meho connector ingest --auth-scheme <name>` (and the REST
  `IngestRequest.auth_scheme` / MCP `meho.connector.ingest` fields) let an
  operator select a named auth scheme from the **closed** catalog (`basic`,
  `static_header`, `session_login`, `session_login_basic`,
  `session_login_token`, `oauth2_mint`) when ingesting an arbitrary spec. The
  register phase then synthesises a minimal `ExecutionProfile` and stamps a
  **dispatchable** `ProfiledRestConnector` instead of the non-dispatchable
  bare shim — staged behind the normal review/enable gate (#1971), never
  auto-enabled. Optional `--auth-secret-field` overrides the secret-field
  NAMES the scheme reads at dispatch; values stay in the target's
  `secret_ref` and never ride the request. Selection only — no free-form auth
  config (login URL / template / token path), and reserved typed-only schemes
  are rejected at the API boundary with a closed-set 422. Omitting the flag
  keeps today's bare-shim behaviour unchanged. (#2271 / #2289)

### Added — profiled-connector boot registration (shipped ExecutionProfiles become dispatchable at boot) (#2288)

- Wire `record_profile_stamp` into production: at boot, immediately after
  the shipped-artifact validator, every catalog row carrying a
  `profile_resource` registers a `ProfiledRestConnector` synthesised from
  its reviewed `ExecutionProfile`, so a shipped profile is dispatchable
  from a fresh deploy instead of inert package data. Idempotent and gated —
  a triple already served by a hand-coded class (vmware/sddc) no-ops, and
  stamping never enables an op (the #1971 review gate stays the interlock).
  Runs as a system operator with no network I/O. (#2271 / #2288)

### Added — `session_login_token` named auth scheme (#2287)

- **A new vetted member of the closed auth-scheme catalog covers the
  JSON-body-login → response-body-token → `Bearer` flow** (#2287). An
  `ExecutionProfile` may now declare `auth.scheme: session_login_token`
  (with `secret_fields: [username, password]`): the harness POSTs a JSON
  `{username, password}` credential body to the login endpoint, reads the
  token out of the response body's `accessToken` field, and sends it as
  `Authorization: Bearer <token>` on subsequent requests. SDDC Manager's
  `POST /v1/tokens` — whose HTTP Basic surface the appliance rejects — is
  the first member of the shape. Like the other session schemes it caches
  until a downstream expiry status (default `{401}`) triggers a full
  re-login; there is no refresh-token leg. Every mechanic is a
  code-reviewed per-scheme constant, not a profile knob — a future product
  with a different login path or token field enters as its own catalog
  member, preserving the no-DSL line (#1177). No shipped connector profile
  selects it yet (`sddc_manager` still ships `basic`); flipping a profile
  onto it is a separate task.

### Fixed — non-finite `INGEST_JOB_TIMEOUT_SECONDS` can no longer defeat the ingest-job watchdog; the budget is now a chart value (#2318)

- **`INGEST_JOB_TIMEOUT_SECONDS=inf` (or `nan`) no longer silently
  disables the async ingest-job watchdog** (#2318, hardens #2275). The
  env loader's only sanity check was `value <= 0`, which a non-finite
  float slips past (`inf <= 0` is `False`; every `nan` comparison is
  `False`), so the value flowed straight into `asyncio.timeout(...)` —
  where `inf` schedules no deadline and `nan` is ill-defined —
  re-opening the exact "job wedged at `status=running` forever" hole
  #2275 was built to close. The loader now rejects non-finite values
  with `math.isfinite`, logs a warning, and falls back to the 1800 s
  default, so a misconfigured budget can never disable the watchdog.
- **The ingest-job watchdog budget is now a first-class Helm chart
  value** (#2318). `config.ingestJobTimeoutSeconds` (schema-typed
  optional string) renders `INGEST_JOB_TIMEOUT_SECONDS` on the backplane
  ConfigMap — injected via the existing `envFrom.configMapRef` — so an
  operator who needs a longer ceiling for a slow shared executor or a
  large spec fleet no longer has to reach for the untyped `extraEnv`
  escape hatch. Rendered only when set: the default `""` omits the env
  so the backend's own 30-min default applies silently (unlike the
  always-render-`""` SSRF-allowlist pattern, an empty numeric env would
  hit `float("")` and warn on every boot). Existing installs are
  unchanged. (#2318)

### Fixed — async ingest jobs always reach a terminal state (watchdog + bounded LLM client + job-id log binding) (#2275)

- **A wedged async connector ingest can no longer sit at `status=running`
  forever** (#2275). An ingest job whose pipeline hit a *never-completing
  await* — a starved `to_thread` executor, a DB connection that never
  acquires, or a grouping LLM call pending on the Anthropic SDK's default
  10-min-read × 2-retry ceiling (~30 min wall-clock) — was stranded at
  `running` until a pod restart cleared the in-memory registry, even
  though the identical *sync* request returned a clean 400. (The
  exception boundary was already maximal — an `OpIdCollision`-raising
  pipeline does flip to `failed` — so the earlier "widen the try/except"
  framing was a no-op; the real defect was the missing watchdog.)
  `run_ingest_job` now time-boxes the whole job body — the pipeline call
  **and** the post-run dispatchability probe — inside
  `asyncio.timeout`; at the deadline the job flips to `failed` with
  `error_class="TimeoutError"`. The budget defaults to 30 min and is
  env-overridable via `INGEST_JOB_TIMEOUT_SECONDS`. Cancellation cannot
  interrupt an already-running `to_thread` OS thread, so the guarantee is
  job-state terminality, not thread reclamation.
- **The grouping LLM client is now constructed with an explicit request
  timeout + retry ceiling** (120 s × 1 retry) instead of the SDK
  defaults, so a hung grouping call fails fast rather than consuming the
  watchdog budget; the fail-closed 503 contract for a missing
  `ANTHROPIC_API_KEY` (#1386) is unchanged.
- **Pipeline log events now carry `ingest_job_id`.** The job id is bound
  via structlog `contextvars` (not just the job coroutine's own logger),
  so the configured `merge_contextvars` processor stamps it onto pipeline
  events (`ingestion_pipeline_start` &c.) — a job-id-filtered log grep is
  no longer blind to the pipeline. No schema or route change. (#2275)

### Added — first-class Helm chart value for the target-SSRF allowlist (#2240)

- **The target-destination SSRF allowlist is now a typed Helm chart
  value** (#2240). v0.20.0 shipped the default-deny target-destination
  SSRF guard whose only opt-out is the `MEHO_TARGET_SSRF_ALLOWLIST` env
  var ("Deployment impact — action likely required"), but the chart had
  no surface for it: operators had to reach for the untyped `extraEnv`
  escape hatch, which `values.schema.json` does not validate and
  `helm show values` does not surface. A new `config.targetSsrfAllowlist`
  value (schema-typed optional string, default `""`) now renders into
  `MEHO_TARGET_SSRF_ALLOWLIST` on the backplane ConfigMap — injected
  into the container via the existing `envFrom.configMapRef` — so a
  scoped allowlist (e.g. `"10.0.0.0/8,192.168.0.0/16"` for a deploy that
  registers on-prem appliances on RFC 1918 space) is a first-class,
  `helm show values`-discoverable setting. The default `""` is a genuine
  no-op that keeps the guard fully on (default-deny), so existing
  installs are unchanged. Chart-only — the guard itself is untouched.
  (#2240)

### Changed — scheduler `fire_at` tick-quantization latency documented on the API schema (#2245)

- **The scheduler's fire-time latency window is now part of the API contract,
  not just an internal doc** (#2245). `ScheduledTriggerCreate.fire_at` and
  `ScheduledTriggerRead.{fire_at,next_fire_at,last_fired_at}` gained OpenAPI
  field descriptions stating that a requested fire time is a **floor, not an
  exact dispatch instant**: the loop scans on a fixed grid every
  `SCHEDULER_TICK_INTERVAL_SECONDS` (default 30 s, env-tunable 1–3600 s) and
  fires on the first tick at or after the requested time, so dispatch can trail
  it by up to one whole tick interval — and `last_fired_at` reads back
  tick-aligned because it is stamped with the claiming tick, not the requested
  time. This is the contract behind an operator-reported "~28 s delay" that was
  pure grid quantization, not failure fallout (no backoff constant exists;
  per-fire failure isolation already shipped in PR #1509 / v0.11.0). SLA-
  sensitive deployments can lower the tick interval (floor 1 s) per deployment.
  Consumer doc (`docs/codebase/scheduler.md`) and the generated CLI client
  regenerated from the new descriptions; **no behavioral change**. (#2245)

### Fixed — crashed ingest no longer strands retry-blocking descriptor debris; `op_id_collision` names its remedy

- **A crashed connector ingest now leaves zero persisted operations for the
  failed spec, and the `op_id_collision` error names how to recover** (#2273).
  Registration committed the `endpoint_descriptor` rows one-per-operation, so
  an ingest that failed partway (e.g. the #2272 YAML crash) left the
  already-processed ops committed as debris; a retry that presented the same
  spec under a *different* URI then aborted with a 400 `op_id_collision`
  against that debris — with no hint of the way out. Registration now commits
  **once per spec** (a single unit of work), so a mid-batch failure rolls back
  to zero rows and the retry inserts cleanly. When a genuine cross-call
  collision does fire, the error — on all three transports (REST 400 `detail`,
  MCP `-32602` `data`, async-job `error`) — now names both remedies: re-ingest
  under the **original** spec URI (a same-URI re-ingest updates in place), or
  clear stranded debris with the `meho.connector.delete` MCP tool (`tenant_id`
  omitted for the global scope). The embedding pass now runs inside one
  transaction per spec (a large spec holds it open for tens of seconds — an
  accepted correctness-first trade-off). No schema or route change. (#2273)

### Fixed — shared modal controller ignores htmx's detached pre-swap target

- **The app-shell modal controller no longer calls ``showModal()`` on a
  disconnected dialog after an ``outerHTML`` swap** (#2242). htmx 2.0.9
  dispatches ``htmx:afterSwap`` with ``detail.target`` still pointing at the
  PRE-swap element, which an ``outerHTML`` swap already detached — the runbook
  run driver's abort / reassign / advance forms target ``#runbook-run-step``
  with ``hx-swap="outerHTML"``. The shared controller scanned that stale
  subtree and reopened the old, closed descendant dialog (the admin-only
  reassign dialog on the abort repro; the assignee's advance dialog on every
  Advance click), throwing an uncaught ``InvalidStateError`` into the console.
  A one-line ``isConnected`` guard on the scan root skips a detached root and
  is behaviour-preserving for the console's ``innerHTML``-into-a-stable-
  container auto-open pattern, which always delivers a connected root. Console
  noise only; the abort / advance actions were always functional.

### Fixed — exactly-one-resumer claim for run-bound approvals (#2293)

- **A run-bound approval now executes its gated op exactly once — no silent
  non-execution, no double dispatch** (#2293). Two opposite seams are closed
  by one `approval_request.resumed_at` claim column (migration `0055`): a
  single conditional `UPDATE ... WHERE resumed_at IS NULL` that every
  resumer of an approved op must win before it re-dispatches `_approved=True`
  (the in-process agent waiter, the shared `resume_dispatch_after_approval`
  operator path, any future resumer). Before this, REST `/decide` and the
  MCP by-id approve *skipped* re-dispatch whenever `run_id` was set, assuming
  the in-process broadcast waiter (#1117) would resume — so when the waiter
  was gone (wait-timeout, pod restart, run cancelled) the approval committed,
  the audit said "approved", and **nothing ran**; meanwhile REST `/approve`
  and the UI approve re-dispatched unconditionally, so with the waiter alive
  one approval could dispatch an approval-gated **write twice**. `/decide`
  and MCP now fall back to a server-side re-dispatch when the claim is free
  (covering waiter-gone), while the same claim blocks the `/approve` / UI
  double-dispatch when the waiter is alive; a resumer that loses the claim
  no-ops cleanly (`dispatch_status="already_resumed"`, HTTP 200). Exactly one
  execution audit row per approval, still stamped with the requester sub. No
  route/schema change beyond the migration. (#2293)

### Added — Discard verb on draft runbooks in the operator console

- **A `tenant_admin` can now discard an unwanted draft runbook template from
  the operator console** (`/ui/runbooks/<slug>`) — the draft close-out leg the
  engine's Discard verb (#2127) already exposed on REST/MCP but the UI had no
  affordance for. A draft's action row gains a **Discard** button behind the
  standard confirm dialog; confirming deletes the draft and `HX-Redirect`s back
  to the catalog. Discard is draft-only: a published / deprecated version is
  retired via **Deprecate** (preserving lifecycle history), and Deprecate stays
  hidden on drafts — deprecate-on-draft is engine-illegal (`draft → published →
  deprecated` is one-directional). (#2241)

### Fixed — catalog re-ingest is idempotent instead of colliding with its own rows

- **A second `meho connector ingest --catalog <product>/<version>` on the
  same deploy now returns an idempotent skip instead of a 400
  `op_id_collision` against the rows the first ingest just wrote** (#2274).
  The catalog shipped-spec on-ramp labels its source `spec:<resource>`;
  the parser then persists that label verbatim as a row tag while the
  upsert appends its own `spec:<spec_source>` marker, so a catalog row
  carries two `spec:` tags. The cross-source collision guard recovered the
  persisted source from the *first* matching tag — the verbatim one, one
  `spec:` layer short of the real source — so every re-ingest mismatched
  its own rows and aborted the batch, making the unbacked-composite
  remediation loop (which prints exactly that command) circular. The guard
  now reads the authoritative *last* (marker) tag and compares on the
  prefix-normalized logical spec identity; genuine cross-source `op_id`
  clashes (`file:///a.yaml` vs `spec:b.yaml`) still fire, and the
  structured exception still carries the raw persisted/incoming labels for
  diagnostics. Compare-time fix only — no data migration, no schema
  change. (#2274)

### Fixed — vendor YAML date/timestamp `example:` values no longer crash spec ingest

- **Ingesting a YAML OpenAPI spec whose schema `example:` fields carry
  unquoted ISO dates or timestamps no longer crashes the descriptor
  INSERT** with `StatementError: Object of type datetime is not JSON
  serializable` (#2272). Stock PyYAML applied the YAML 1.1 implicit
  `timestamp` resolver, turning an unquoted `2000-01-23T04:56:07.000+00:00`
  into a `datetime` (and `2024-01-15` into a `date`) that the JSON(B)
  descriptor columns cannot encode; the spec-ingest loader now keeps such
  scalars as the verbatim string the author wrote (OAS 3.1 limits YAML
  tags to the JSON Schema ruleset, which excludes the timestamp tag).
  Ingest also gained a fail-closed serializability check at the shared
  proto-build boundary, so `dry_run=true` now rejects a non-encodable
  spec with the same structured `invalid_schema` 400 the real run raises
  instead of green-lighting it. Scoped to spec ingest — no engine-wide
  serializer change. (#2272)

### Removed — the L2 composite failure-coping apparatus (two-world op model)

- **The dispatch-time L2 pre-flight and its structured errors are deleted, not
  guarded** (#2259). Now that every code-shipped composite dispatches its
  sub-ops directly on the connector session (#2253/#2255/#2256), nothing
  depends on ingested catalog rows, so the whole coping apparatus is retired:
  vmware's `preflight_l2_dependencies` + the `composite_l2_missing` /
  `composite_l2_disabled` error codes (and their `CompositeL2Dependency*`
  exceptions), github's import-time `UnbackedEnabledCompositeError` load guard
  and its `composite_backing` registry, and the `unbacked` / `next_step`
  markers `search_operations` attached to composite hits. The platform-wide
  registration-time invariant (#2252) is the sole remaining check: a
  code-shipped op whose declared `dispatch_child` sub-op resolves to an
  ingested row still fails the boot closed. Part of the two-world op model
  (Goal #2247, Initiative #2248). Operator-visible change: the
  `composite_l2_missing` / `composite_l2_disabled` error envelopes and the
  `unbacked=true` search-hit flag no longer appear — composites simply work on
  a fresh boot with zero catalog ingest.

### Changed — vmware `host.vsan_health` + `host.network_uplinks` re-shipped as typed ops

- **`vmware.host.vsan_health` and `vmware.host.network_uplinks` are now
  `source_kind="typed"` bound-method ops, and the two composites
  (`vmware.composite.host.vsan_health` / `vmware.composite.host.network_uplinks`)
  are removed** (#2258): both now read directly on the connector session via
  `RetrievePropertiesEx` / `VsanQueryVcClusterHealthSummary`, mounted through
  `mount_op_path`, in the `vmware.host.usage` (#2257) mould — one row per
  host/cluster, working on a fresh boot with **zero catalog ingest**. Output
  is byte-for-byte compatible with the composite versions, including the
  per-host / per-cluster best-effort `read_note` on a partial failure. The
  registered vmware composite total drops 15→13 (7→5 read); the two reads
  keep `safety_level="safe"` / `requires_approval=False`. Part of the
  two-world op model (Goal #2247, Initiative #2250). No new auth model —
  `vmware-rest-9.0` stays State 2.

### Added — GCP Secret Manager backend Phase 2 (per-operator Workload Identity Federation)

- The `gsm` credential backend now supports a **per-operator** read path via
  Keycloak→GCP-STS Workload Identity Federation. When `GSM_WIF_AUDIENCE` is
  set, a `gsm:` read exchanges the operator's Keycloak JWT
  (`operator.raw_jwt`) at `sts.googleapis.com` for a short-lived federated
  token (google-auth `identity_pool.Credentials`), optionally impersonates
  `GSM_WIF_SERVICE_ACCOUNT`, does the one `secretmanager.versions.access`,
  and discards the token — so GCP's own audit log attributes the read to the
  operator, mirroring Vault's per-operator JIT contract. A fresh credential
  is minted per read (never cached). Installs that leave `GSM_WIF_AUDIENCE`
  unset keep the Phase-1 SA-direct path unchanged. The GCP-side Workload
  Identity Pool + OIDC provider must trust the MEHO Keycloak issuer; no
  service-account key material is ever used. New `GSM_WIF_AUDIENCE` /
  `GSM_WIF_POOL_ID` / `GSM_WIF_PROVIDER_ID` / `GSM_WIF_SERVICE_ACCOUNT` /
  `GSM_WIF_SUBJECT_TOKEN_TYPE` settings; the Helm surface lands in #2231.
  (#2232)

### Added — GCP Secret Manager credential backend (Phase 1, SA-direct)

- Resolve a target's `gsm:<project>/<secret>[#field]` `secret_ref` through
  a new `gsm` credential backend on the #2229 resolver seam, reading GCP
  Secret Manager under MEHO's own GKE Workload Identity ADC (optionally
  impersonating a configured SA via `GSM_IMPERSONATE_SA`) — a Vault-free
  install path for GCP-native adopters. A bare ref returns the whole JSON
  payload; a `#field` fragment selects one key. No service-account JSON
  key is ever used (honours `constraints/iam.disableServiceAccountKeyCreation`).
  Per-operator GCP federation and the Helm surface are deferred to #2232 /
  #2231. (#2230)

### Added — Helm `gsm.*` surface + backend-agnostic `/api/v1/health` federation proof

- Deploy a **Vault-free, GCP-native** install: the chart gains a top-level
  `gsm.*` block (`enabled`, `project`, and inert Phase-2 (#2232)
  `workloadIdentityFederation.{poolId,providerId,serviceAccount}` stubs)
  plus `config.credentialBackend` / `config.gsmProject` /
  `config.gsmImpersonateSa`, rendered into the backplane env as
  `CREDENTIAL_BACKEND` / `GSM_PROJECT` / `GSM_IMPERSONATE_SA`.
  `values.schema.json` now requires `vault.address` **only when**
  `config.credentialBackend: vault` and requires `gsm.enabled: true` +
  `gsm.project` when it is `gsm`, so a GSM-only overlay passes schema
  validation with a blank Vault address (see
  `deploy/values-examples/values-gsm-example.yaml`). (#2231)
- The `GET /api/v1/health` federation proof is now **backend-agnostic**:
  it dispatches on `config.credentialBackend` — a Vault install keeps the
  unchanged `vault.kv.read` path (zero migration), while a GSM install
  reads its probe secret (`gsm:<project>/meho-test-federation`) through the
  credential-backend seam and reports the same `vault` status shape. (#2231)

## [0.20.0] - 2026-07-08

### Fixed — ssh-family connectors resolve `secret_ref` from Vault

- **The SSH adapter now resolves `target.secret_ref` (a Vault KV-v2 path
  string) through the operator-context Vault read, instead of consuming
  the column as if it were the already-materialized secret dict**
  (#2155): every ssh-family op (`holodeck-ssh`, `bind9-ssh`,
  `pfsense-ssh`) failed in ~5ms against any real registered target with
  `AttributeError: 'str' object has no attribute 'get'` — the adapter
  implemented the documented "bind9 anti-shape" and never resolved the
  Vault path at all. `SshConnector._auth_config` now reads the secret
  via `_shared.vault_creds.load_vault_secret_data` under the request
  operator's identity (the same seam the REST connector loaders use),
  the operator is threaded through every ssh-family op handler +
  `_run_command` / `pwsh_run` / the bind9 sudo-password lookup, and
  operator-less paths (`probe()`, readiness) fail closed at Vault.
  pfSense's key-only refusal is preserved. `holodeck.k8s.exec` now
  surfaces auth/transport failures as a `connector_error` (mirroring
  `holodeck.about`) rather than swallowing them into a `status: "ok"`
  envelope with empty stdout. The `secret_ref` example in
  `docs/cross-repo/holodeck-onboarding.md` is corrected to a
  mount-relative path inside the meho-readable `secret/meho/*` subtree.

### Fixed — runbook run list surfaces the failed step state

- **`GET /api/v1/runbooks/runs` (and `meho.runbook.list_runs`) now
  project `current_step_state` (`in_progress` / `failed`) on each run
  summary** (#2119): a `"no"` / `"escalate"` answer to a step's
  `confirm` verify has always persisted the step as `failed`, but the
  read surface kept reporting only `state: "in_progress"` +
  `current_step_id` — operators and monitoring could only discover the
  failure by firing *another* `next` mutation and parsing the 400.
  The persisted per-step state now rides the existing list projection
  (additive, optional field; step *contents* remain opaque per the
  Initiative #1198 adherence floor), so a poller can see "stuck on a
  failed step; abort is the only forward path" without mutating the
  run. OpenAPI snapshot + generated CLI client regenerated.

### Added — `/ready` surfaces the approval four-eyes posture

- **`GET /ready` → `features.approval_queue` now carries an
  `effective_posture` field** (#2087): `"four_eyes_enforced"` on the
  fail-closed default, `"single_operator_break_glass"` when the deploy
  set the emergency `APPROVAL_ALLOW_SELF_APPROVAL=true` escape — one
  GET tells an operator or auditor whether self-approval is currently
  possible, without grepping the values file.
  `docs/codebase/approvals.md` now also documents how to audit a
  break-glass self-approval to code truth: the flag emits the
  `approval_self_approval_break_glass` WARNING log event and the
  decision audit row identifies a self-approval as
  `payload.principal_sub == payload.reviewed_by` — there is **no**
  `self_approved` audit field (a first-class marker would be net-new,
  separately scoped work), and the endorsed single-operator posture
  remains the agent-requester recipe, not the flag.

### Fixed — approval-gated dispatches reconstruct on the session-replay surface

- **`GET /api/v1/audit/sessions/{id}/replay` now sees approval-gated
  chains** (#2086): the park → decide → execute audit rows carried
  neither an `agent_session_id` anchor nor a `parent_audit_id`
  back-link (the approve/resume surfaces run on a different task than
  the parking dispatch, so the session contextvars were gone by
  decision time), leaving the whole chain invisible to replay
  (`{root: [], row_count: 0}`) even with
  `MCP_REQUIRE_SESSION_ID=enforced`. The parked `approval_request` row
  now persists the originating session id and the parking audit row's
  id (migration `0053`, same durability mold as `work_ref` / #1659);
  the decision audit rows and `resume_dispatch_after_approval`'s
  re-dispatch re-hydrate both, so an approved chain replays as one
  subtree — the `approval.request` root with the decision and the
  executed dispatch as its children — and the executed row honestly
  records which session asked for it.
### Fixed — agent runs fail closed on unexecutable runbook instructions

- **An agent run whose prompt instructs execution of a runbook no longer
  reports `succeeded` on a zero-tool-call no-op with a hallucinated
  explanation** (#2077): the agent meta-tool catalog contains no
  runbook-execution tool (`meho.runbook.*` are operator MCP verbs, and
  `confirm`-gated steps require a human answer by design), so the run now
  fails typed at run start — before any model call — with a
  machine-classifiable `unexecutable_runbook_reference:`-prefixed error on
  the run outcome (`meho.agents.run`, `GET /agents/runs/{handle}`, and one
  terminal `error` SSE frame on the events stream). Prompts that merely
  mention runbooks are unaffected; a future agent-executable runbook tool
  hooks in via `RUNBOOK_EXECUTION_META_TOOL_NAMES`.
### Fixed — MCP async ingest rejects a foreign tenant_id eagerly

- **`meho.connector.ingest` with `async: true` and a foreign
  `tenant_id` now returns an immediate structured JSON-RPC `-32602`
  denial — the same envelope and message as the sync MCP path —
  instead of a success-shaped job handle plus a `failed` job the
  caller could never poll** (#2214; the MCP twin of REST #2208): the
  tool handler runs the shared tenant-scope predicate
  (`IngestionPipelineService.authorize_scope`) eagerly, before the
  inline/async branch split, so no job row is minted and no pipeline
  task is detached for an unauthorised write scope. The denial rides
  the MCP error envelope (there is no HTTP-403 on that transport) and
  carries the same diagnostic string as the REST 403 `detail`.
  Previously the async path minted the job under the *requested*
  foreign tenant — unpollable via the tenant-scoped
  `meho.connector.ingest_status` gate — and the sync path's
  `PermissionError` degraded to an opaque `-32603 "internal error:
  PermissionError"`. The service-layer guard remains as
  defence-in-depth; no REST/OpenAPI change (MCP-only).

### Fixed — operator console surfaces the topology no-populator coverage gap

- **The console's topology refresh partial now renders the #2093
  coverage-gap signal**: when a refreshed target's connector ships no
  topology populator (`no_populator_for_product` set on the
  `RefreshResult`), the result fragment shows a warning callout naming
  the product and listing the registered products that do ship a
  populator — matching the CLI note — instead of bare all-zero counts
  that read as a clean no-op. Server-rendered Jinja conditional only;
  no route, HTMX, or OpenAPI change. (#2210)
### Added — tenant-scope affordance on the CLI + UI ingest on-ramps

- **`meho connector ingest` gains `--tenant-id` and the
  `/ui/connectors/registry` ingest modal gains a Write-scope select**
  (#2209): both human on-ramps can now target a tenant-curated ingest
  without hand-crafting raw REST/MCP bodies. Omitting the flag (or
  picking "Global") keeps the built-in / global scope — the
  omit-equals-global contract the REST route + MCP tool share since
  #2085; the UI derives the tenant UUID server-side from the
  authenticated operator, and the CLI validates the UUID locally
  before any request leaves the machine.
### Fixed — async ingest rejects a foreign tenant_id synchronously

- **`POST /api/v1/connectors/ingest` with `async: true` and a foreign
  `tenant_id` now returns a synchronous 403 — the same status and body
  shape as the sync branch — instead of `202 Accepted` plus a `failed`
  job the caller could never poll** (#2208; hardens the #2085 / PR
  #2204 write-scope semantics): the route runs the shared tenant-scope
  predicate (`IngestionPipelineService.authorize_scope`) eagerly,
  before the async branch mints its job row, so no job is created and
  no pipeline is detached for an unauthorised write scope. Previously
  the job row was minted under the *requested* foreign tenant, making
  the poll on `GET /api/v1/connectors/ingest/jobs/{id}` 404
  cross-tenant — "authz denied" was indistinguishable from "pipeline
  crashed". The service-layer guard remains as defence-in-depth for
  the CLI / MCP siblings; the 403 response is now declared in the
  route's OpenAPI response map (CLI snapshot + Go client regenerated).

### Fixed — operations/call target failures all ride the 200 envelope

- **`POST /api/v1/operations/call` (and `/preview`) now honor the
  documented "always HTTP 200 + dispatcher envelope" contract across
  all six target-failure modes** (#2110, Option A; completes #136 /
  #2130): a `target` of a wrong JSON type (e.g. `target: 12345`) — the
  last mode that still escaped as a FastAPI 422 `detail[]` array —
  returns `status="error"` with
  `extras.error_code="target_invalid_type"` and the offending JSON-type
  name in `extras.received_type`. Consumer error handling is now a
  single switch on `extras.error_code`
  (`target_required` / `target_invalid_type` / `no_target` /
  `ambiguous_target`), invariant across typed connectors. The published
  OpenAPI schema for `target` is unchanged (`string | object | null`),
  so generated clients don't churn; the same seam also fixes a latent
  MCP-transport crash where a non-string/dict `target` hit
  `AttributeError` instead of a structured envelope.

### Added — target secret_ref tenant-scope fail-fast + cause-named Vault denial

- **`POST`/`PATCH /api/v1/targets*` (which `meho targets import` drives)
  now reject an explicit `secret_ref` outside the operator's readable
  per-tenant subtree** with a structured 422
  (`kind="secret_ref_outside_tenant_scope"`) naming the constraint, the
  rendered tenant prefix, and the exact expected
  `tenants/<tenant_id>/<name>` path — a target with such a ref imported
  clean and then failed every dispatch with an opaque Vault `permission
  denied`. Semantics mirror the runtime tenant-scope guard
  (segment-boundary match on the mount-pinned candidate); the derived
  per-tenant default (#1723) and an explicit-null clear are untouched,
  and the gate is a no-op when the guard is disabled
  (`VAULT_KV_TENANT_SCOPE_PREFIX=""`). (#2091)
- **An `hvac` `Forbidden` during dispatch now maps to the structured
  `connector_vault_forbidden` error** instead of the bare
  `connector_error: Forbidden` passthrough: the message names the
  target's `secret_ref`, the likely out-of-subtree cause, the
  per-tenant convention with the exact expected path, and the
  stage-the-credential remediation — with an explicit warning that
  widening the deploy-owned Vault policy is the wrong fix. A
  target-less denial (typed `vault.*` op rejected by the Vault ACL)
  gets a generic Vault-authorization shape; other hvac errors fall
  through to `connector_error` unchanged. (#2091)




### Fixed — topology refresh returns structured errors instead of a bare 500

- `POST /api/v1/topology/refresh/{target_name}` no longer returns a bare
  `500 text/plain "Internal Server Error"` when no registered connector
  supports the target's `product` (e.g. a legacy pre-standardization
  `kubernetes` slug where the connector registers as `k8s`): the route
  now maps the refresh service's connector-resolution exceptions to
  structured JSON — `NoMatchingConnector` → **422
  `no_matching_connector`** (with the offending `product` and the
  resolver message) and `AmbiguousConnectorResolution` → **409
  `ambiguous_connector`** (with the `(product, version, impl_id)`
  candidates so the caller can set `target.preferred_impl_id` and
  retry). Both error envelopes are declared in the OpenAPI spec so the
  generated CLI/SDK pick them up. The sibling silent-no-op case (a
  resolvable connector without topology support returning all-zero
  counts) is tracked separately in #2093. (#2092)

### Fixed — topology refresh names the populator coverage gap instead of a silent all-zero no-op

- `POST /api/v1/topology/refresh/{target_name}` against a target whose
  `product` ships **no topology populator** (the connector inherits the
  base `discover_topology` no-op — e.g. `argocd`, `vault`, `keycloak`,
  `vrli`, `gh`, `vmware`) no longer returns an all-zero count body
  indistinguishable from a clean populator run: the `RefreshResult` now
  carries `no_populator_for_product` (the offending product slug) plus
  `populated_products` (the registered products that DO refresh
  meaningfully, e.g. `["k8s"]`). Both fields are `null` when a
  populator ran — including a run that legitimately reconciled zero
  changes — so a consumer tells no-op-by-design from coverage gap with
  a single field check instead of tracing meho source. Non-breaking:
  HTTP status stays 200 and the six counts are unchanged; the reconcile
  (including soft-deletes of previously adopted nodes, the audit row,
  and the broadcast event) still runs. The `meho topology refresh` CLI
  verb appends a human-readable note when the signal is set. (#2093)
### Breaking changes — REST connector-ingest omitted `tenant_id` now targets the global scope

- **`POST /api/v1/connectors/ingest` with no `tenant_id` in the body now ingests under the built-in / global scope (`tenant_id IS NULL`), matching the MCP tool `meho.connector.ingest`'s documented "omit = global" semantics** (#2085): previously the REST route silently resolved omission to the *caller's JWT tenant*, so a consumer following the MCP-documented body minted a caller-tenant shadow copy of an existing global row (the scope-aware dedup never matches across scopes; the v0.14.0 dogfood hit this as a 136-op duplicate). The request schema gains an optional `tenant_id` field with a documented resolution: omitted / `null` → global (tenant_admin — already the route's gate), your own tenant UUID → tenant-curated scope, any other UUID → 403. **Migration recipe:** clients that relied on the old caller-tenant default add `"tenant_id": "<your-tenant-uuid>"` to the ingest body; bodies without it now write global rows. The `meho connector ingest` CLI verb and the `/ui/connectors/registry/ingest` modal drive this route and inherit the new global default.

### Security — fail-closed on unrecognized `principal_kind` claim

- JWT verification now rejects a token whose `principal_kind` claim is
  present but outside the closed `user`/`service`/`agent` enum with a
  `401` (`unknown_principal_kind`) instead of silently treating the
  principal as a human user. `principal_kind` is the discriminator the
  per-kind permission model, approval gate, and agent dispatch branch
  on, so an out-of-enum issuer-signed value now fails closed — exactly
  like an unknown `tenant_role` at the same layer. The structured
  `unknown_principal_kind` warning (claim name + offending value) is
  still logged before the rejection. Tokens that **omit** the claim
  entirely are unaffected: the pre-G11.2 absent-claim → `user` legacy
  fallback is kept, so existing human-operator tokens keep working
  without a Keycloak mapper update. Realms emitting a custom kind must
  widen the enum first rather than relying on the silent coercion.
### Security — /health least-privilege split (OPERATOR gate + liveness probe)

- **`GET /api/v1/health` (the federation-proof deep check) is now
  gated at `TenantRole.OPERATOR`**, and a new cheap liveness probe
  ships at **`GET /api/v1/health/live`** for low-privilege /
  monitoring callers (evoila-bosnia/meho-internal#159). Every
  `/api/v1/health` call drives a live Vault JWT/OIDC credential
  federation plus a KV v2 secret read under the caller's identity;
  it previously required only a valid JWT, so the lowest-trust
  `read_only` rank (what monitoring principals are expected to hold)
  could trigger that Vault round-trip on every poll. A `read_only`
  caller now receives 403 `insufficient_role` *before* any Vault
  interaction; `operator` / `tenant_admin` callers (the CLI `meho
  status` audience, the install smoke) see the exact same response
  as before. **Migration for `read_only` monitoring callers**: poll
  `GET /api/v1/health/live` instead — valid JWT (any tenant role),
  returns `{operator, db}` (identity + DB-migration liveness), no
  `vault` field, and its code path never touches Vault or the
  dispatcher (pinned by a source-level guard test). Kubernetes
  liveness/readiness probes are unaffected — they target the
  unauthenticated `/healthz` / `/ready` chassis routes. The MCP
  `meho.status` tool is unchanged (explicitly out of scope here).
  OpenAPI snapshot + generated CLI client regenerated for the new
  route.
### Security — targets test fixture secret-ref hygiene

- Align the internal-lab targets test fixture
  (`backend/tests/fixtures/rdc-hetzner-dc-targets.yaml`) with its own
  Vault-`secret_ref`-only convention: three operator-note remnants that
  inlined a well-known lab-default credential string are replaced with
  rotate-and-store-in-Vault guidance pointing at each target's
  `secret_ref`, so the fixture models the secrets-stay-in-Vault pattern
  end to end and stays trivially clean under the zero-tolerance
  TruffleHog secret scan. Fixture prose only — no entry added or
  removed, no code or schema change.
  (evoila-bosnia/meho-internal#158)
### Security — runbook verify dispatch fails closed on Vault-backed credential reads

- **The runbook `operation_call` verify dispatch's synthetic operator now carries an empty `raw_jwt`, so an operator-context Vault credential read refuses locally before any Vault contact** (evoila-bosnia/meho-internal#157): the runbook run service reconstructs a minimum-shape `Operator` from `operator_sub` for the verify dispatch; it previously carried a non-empty placeholder `raw_jwt` that bypassed the shared credential reader's empty-JWT fail-closed guard (`_resolve_secret_ref`), so the placeholder was forwarded to Vault's JWT/OIDC login and only rejected server-side after a live network round-trip. The synthetic operator now follows the empty-string synthetic-operator convention (same shape as the topology scheduler's refresh operator): a verify `op_id` resolving to a Vault-backed connector fails closed with a structured `VaultCredentialsReadError` refusal before Vault is touched, the step transitions to `failed`, and the dispatched verify's audit identity (`sub` / `tenant_id`) is unchanged. Non-Vault typed in-process verify ops dispatch exactly as before. Hardening only — no FastAPI route/schema change, OpenAPI snapshot unchanged.
### Security

- The MCP `tools/call` argument gate now asserts the `format` keywords
  declared in tool `inputSchema`s (`uuid`, `date-time`): the
  jsonschema validation call passes
  `format_checker=Draft202012Validator.FORMAT_CHECKER` (with the new
  `rfc3339-validator` dependency registering the `date-time` checker),
  so a malformed UUID or non-RFC-3339 timestamp is rejected as JSON-RPC
  `-32602` "Invalid params" before any tool handler runs — previously
  `format` was annotation-only and such values reached in-handler
  parsers, surfacing as `-32603` "Internal error" and defeating agent
  self-correction. In-handler UUID/timestamp re-parses stay in place as
  defense-in-depth.
### Security — CI supply-chain input pinning + Go CVE scanning

- **Pinned the release/CI supply-chain inputs that still floated, and
  added Go dependency CVE scanning** (#155): the GoReleaser build used
  by the signed CLI release pipeline is pinned to the exact patch
  `v2.15.4` (was `~> v2`, i.e. "latest v2.x at run time"), matching
  `cli/Makefile`'s `GORELEASER_VERSION` so CI releases and the local
  `make release-dry-run` use the identical GoReleaser build; the
  testcontainers VCSim image is pinned to the `v0.55.1` release tag
  (was `:latest`) in all three CI jobs, matching the pinning
  convention of the sibling pgvector/valkey/Vault test images; and the
  `go-lint-test` CI job now runs `govulncheck ./...` (pinned
  `golang.org/x/vuln@v1.5.0`) against the `cli/` module, so known CVEs
  reachable from the CLI's call graph fail CI instead of going
  unnoticed. Workflow + docs only — no runtime code change.
### Security — untrusted-content envelope on stored agent text

- Agent-authored stored text is now re-served to LLM-facing read
  surfaces inside a positional guard/delimiter envelope
  (`<<UNTRUSTED_AGENT_TEXT … END_UNTRUSTED_AGENT_TEXT>>`, mirroring the
  tenant-conventions preamble wrapper): broadcast announcement
  `activity`/`scope`/`target` on `meho.broadcast.recent`,
  `meho.broadcast.watch` and the `meho://tenant/{tenant_id}/feed`
  resource, plus the full Markdown `body` on the `meho://kb/{slug}` and
  `meho://memory/{scope}/{slug}` resources. The delimiters are never
  derived from the wrapped content, so stored text containing the
  closing-delimiter literal cannot escape the block, and the three
  resource descriptions now advertise the served free-text as
  agent-authored, untrusted, and not a system directive. The
  tenant-feed resource additionally serves agent announcements (it
  previously skipped them as malformed). Structural hardening only —
  no content filtering or detection, stored rows unchanged.
  (evoila-bosnia/meho-internal#154)
### Security — target-destination SSRF guard (create/update + connect)

- **Targets can no longer be pointed at non-public addresses unless the
  deployment explicitly allowlists them** (evoila-bosnia/meho-internal#153):
  `POST`/`PATCH /api/v1/targets` now reject a `host`/`fqdn` that is — or
  resolves to — a private, loopback, link-local (including
  `169.254.0.0/16` cloud-metadata), ULA, multicast, unspecified, or
  otherwise reserved address with a structured 422 that never echoes the
  resolved IP; and the HTTP connector transport re-screens the
  **resolved** address immediately before every dispatch (closing the
  DNS-rebind window between create and connect), refusing with a
  structured `connector_error` before any request or credential leaves
  the backplane. The rejection classes extend the existing OpenAPI
  spec-fetch guard with a `not is_global` posture, so every
  non-globally-routable address — including carrier-grade NAT
  `100.64.0.0/10` — is blocked. Both layers screen the
  **httpx-normalized dialed host**, never the raw string: a `host`
  value carrying URL structure cannot screen as one destination and
  dial another — values embedding credentials, a query, or a fragment
  are refused outright, while path- or port-bearing values (the GitHub
  connector's documented `owner/repo` host shapes stay valid) are
  screened on the host component they actually dial.
  **Deployment impact (action likely required):** MEHO
  deployments register on-prem appliances on RFC 1918 space as a matter
  of course — set the new `MEHO_TARGET_SSRF_ALLOWLIST` env var (comma-
  separated CIDR ranges, bare IPs, and/or hostname literals, e.g.
  `10.0.0.0/8,192.168.0.0/16,vcenter.lab.internal`) to opt your trusted
  internal ranges back in. The allowlist is range-scoped — everything
  outside it stays blocked, so the guard is never globally disabled.
  Existing target rows are unaffected at rest (reads/lists still work);
  the guard bites on the next create, update, or dispatch.
### Security

- Bound the `/mcp` transport and the memory/knowledge MCP tool inputs
  against oversized payloads: `search_memory.query` and
  `search_knowledge.query` now carry a 256-char `maxLength`,
  `add_to_memory.body` (and its deprecated `content` alias) and
  `add_to_knowledge.body` cap at 64 KiB — matching the operator
  console's existing body cap — so oversized free text is refused by
  schema validation before it reaches the retrieval substrate's
  tsvector + embedding indexing. The `/mcp` route itself now enforces a
  1 MiB request-body limit (`Content-Length` fast-reject plus a
  streaming cap for chunked bodies) and returns HTTP 413 with a
  JSON-RPC `INVALID_REQUEST` envelope instead of buffering unbounded
  payloads.
### Fixed — `meho login --resolve` named-port silent no-op

- `meho login --resolve <host>:<port>:<ip>` now rejects named service
  ports (e.g. `kc.example.com:https:10.0.0.5`) loudly at parse time:
  the override map is keyed by the numeric dial address, so a named
  port previously passed validation (`net.LookupPort`) but keyed the
  map as `host:https` — never matching the dial address, silently
  ignoring the pin, and violating the flag's fail-loud contract. The
  port must now be strictly numeric (1-65535). An IPv6-literal *host*
  (unrepresentable in the front-split `host:port:ip` format) also gets
  an explicit error instead of a misleading port/IP validation
  failure, and the parser docs now describe the actual front-split
  behaviour. Follow-up to the PR #2181 review. (#2107)

### Security — operator-console read-session token revalidation

- The operator console's `/ui/*` read path now re-validates the
  session's stored access token against the JWKS-cached JWT chain on a
  drift-gated cadence (`UI_SESSION_READ_REVALIDATION_SECONDS`, default
  300; `0` = every request), instead of authenticating reads off the
  decrypted session row alone for the session's full absolute lifetime.
  Past the threshold, an expired token silently refreshes through the
  existing RFC 9700 rotation seam and a token the IdP no longer honours
  (refresh grant rejected, signature/audience failure) bounces the
  operator to login — bounding IdP-side revocation and role-demotion
  lag on read renders to roughly the access-token TTL plus the
  threshold. On a JWKS cache hit the added per-request cost is zero
  outbound calls; long-lived SSE streams honour the same check on
  (re)connect.

### Fixed — /ui/memory tag-datalist URL rewrite on page load

- **The `/ui/memory` tag-autocomplete `<datalist>` no longer rewrites the browser URL to `/ui/memory/tags?tag=&scope=all` on every page load** (#2069): the datalist's `hx-trigger="load"` options fetch inherited the ancestor filter form's `hx-push-url="true"` and `hx-include="closest form"` (htmx 2.0.9 resolves both closest-ancestor-wins, the same inheritance that #1709 had to override for `hx-target`), so each load pushed a stale `/ui/memory/tags` URL into browser history and dragged the form's `tag`/`scope` inputs into the request query string. #1709 (v0.15.0) pinned `hx-target="this"` and fixed the worse grid-clobber half but left these two inherited attributes unscoped. The datalist now also carries `hx-push-url="false"` and `hx-include="none"`, so its load-time fetch leaves the address bar and history untouched and sends no inherited inputs; the card grid (`#memory-cards`) and the options fetch itself are unchanged. The existing regression test now guards all three inherited attributes. Template attribute + test + docs only — no FastAPI route/schema change, OpenAPI snapshot unchanged.

### Changed — connector ingest SSRF rejection (discoverability)

- The ingest SSRF guard's rejection messages (non-`https` scheme and
  non-public destination) now name the inline on-ramp: a spec inside your
  own network can be ingested by passing it as a `file:///path` or
  `docs:<product>-<version>/<file>` source so the CLI uploads it inline,
  instead of having to publish internal specs to the public internet. The
  guard remains default-deny and the message stays path-free (no resolved
  IP / hostname echoed). (#2070)

### Fixed — session-expiry dispatch recovery

- Recover expired connector sessions on the generic-ingested dispatch
  path: an auth-class status (`401` for vCenter REST, `440` for vRLI)
  now triggers a single automatic re-login + retry at the dispatcher's
  `HTTPStatusError` arm instead of hard-failing until a backplane
  restart. Both `VmwareRestConnector` (cold-401) and `VcfLogsConnector`
  (idle-440) — and any session-stateful connector exposing the new
  duck-typed `invalidate_session(target)` hook — recover transparently;
  a second consecutive auth failure still surfaces the actionable
  `connector_auth_failed` error. Retry-once is gated strictly on the
  auth-class set (a `5xx`/timeout is never retried) and a recovered call
  emits exactly one success audit row, no spurious error row.
  (#2067)

### Fixed — RFC6570 {+path} ingest

- Ingest now keys an `in: path` parameter whose declared name carries a
  leading RFC6570 expression operator (e.g. `{"name": "+path"}` for a
  `/v1/{+path}` template) on the **bare** name (`path`), matching the
  dispatch renderer (PR #2020) which strips the operator before looking the
  value up — so these ops dispatch instead of dying with a `KeyError`. The
  operator set is now a single shared constant, so the ingest key and the
  render lookup can't drift again. A spec declaring both `path` and `+path`
  path params fails ingest loudly. `preview_operation` on such an op now
  returns a structured `dispatch_error` envelope instead of an uncaught
  exception / MCP `-32603` (#2066).

### Security

- Hardened the dispatch broadcast feed against classification drift.
  Broadcast request params now run through a Tier-1 pass — a
  secret-shaped key-name scrub plus the deterministic named-pattern
  redactor — before the payload is built, and any secret detection
  collapses the broadcast to aggregate-only regardless of op class, so
  a secret-bearing op missing from the `credential_*` allowlists no
  longer ships its raw params to co-tenant feed subscribers. A new
  classifier-coverage test enumerates every registered typed/composite
  operation and fails CI when an op declaring secret-shaped parameters
  is not pinned to a `credential_*` class. Benign write broadcasts
  keep their full mutation detail.

- Hardened Tier-1 redaction at the connector boundary and the dispatch
  error path. The `authorization_header` / `bearer_token` / `api_key`
  named patterns now capture a labelled secret's value to its natural
  delimiter (whitespace / closing quote / end of blob) instead of
  stopping at the first punctuation byte, so punctuated values are
  redacted whole. The `operations/_errors.py` result builders now run
  every free-text diagnostic (`exception_message`, `upstream_message`,
  `detail`, and the summary tails built from them) through the Tier-1
  redactor **before** the 256-char cap, so a credential embedded in a
  stringified connector exception or upstream error body no longer
  reaches the response/audit envelope in cleartext.

### Added — hetzner-rest connector: minimal spec + live Vault auth

- **`hetzner-rest-2026.04` connector ingests a hand-authored minimal OpenAPI 3.0 spec and un-stubs its Vault credential read** (#2180): the Hetzner Robot Webservice publishes no OpenAPI document, so this ships `hetzner_robot_minimal.yaml` as package data (list/get servers, vSwitch get + membership, per-server firewall get/set, rDNS, `server_addon` order). Ingesting it reuses the hand-coded `HetznerRobotConnector` (no `GenericRestConnector` shim), so ingested ops resolve via `resolve_connector`, and `load_credentials_from_vault` now performs a live operator-context KV-v2 read via the shared `load_basic_credentials` helper (system-operator calls fail closed). Ship-state: **loader-wired (State 2)** — the curated read core is enabled and executes end-to-end against a real Robot account; the declared write ops (vSwitch/firewall mutation, `server_addon` order) are ingested for corpus coverage but **not enabled** (wire-format-faithful write dispatch stays the G3.x write-surface work).

### Added — vmware `host.network_uplinks` read composite

- **`vmware.composite.host.network_uplinks` returns per-host physical NIC link-state/speed and proxy-switch/uplink association** (#2178): reads `config.network.pnic` + `config.network.proxySwitch` off a per-host `RetrievePropertiesEx` — the pnic-to-uplink mapping the plain vSphere Automation REST surface can't reproduce (drives physical switch-port-occupancy reasoning). Registered `safety_level="safe"` / `requires_approval=False` per the #508 read-composite pattern; the per-host read is best-effort (a failed read nulls the network detail with a `read_note` rather than sinking the composite). Lands in the existing `host` group; registered composite total 5→6 read / 13→14 total. No new auth model — `vmware-rest-9.0` stays State 2.

### Added — vmware `host.vsan_health` read composite

- **`vmware.composite.host.vsan_health` surfaces per-cluster vSAN health (test groups + overall status)** (#2187): queries `VsanQueryVcClusterHealthSummary` on the `vsan-cluster-health-system` singleton scoped to the target cluster's `ClusterComputeResource` — the `govc vsan.health.*` equivalent the plain REST surface can't reproduce. Registered `safety_level="safe"` / `requires_approval=False` per the #508 pattern; best-effort (a vSAN-disabled cluster or rejected health call nulls `groups`/`overall_health` with a `read_note`). Registered composite total 14→15 (7 read + 8 write). No new auth model — `vmware-rest-9.0` stays State 2.

### Added — holodeck `disk.usage` read op for pre-eviction disk diagnosis

- **`holodeck.disk.usage` reports root-fs usage (`df -B1 /`) plus `du -sb` over a fixed growth-dir set** (#2168): the complete pre-eviction diagnostic signal for the 74 GB root fs that fills on VCF-9.x backup accumulation (Initiative #2145). The measured dirs (`/var/backups`, `/holodeck-runtime`) are a module constant with **no path parameter** (empty-object schema, `additionalProperties: False`), so it can never become an arbitrary `du`; `safety_level="safe"`, `requires_approval=False`, `read-only` tag. `percent_used` is computed from byte counts (not `df`'s rounded `Use%`); a failed sub-command flips only that entry's `ok`. Op count for `holodeck-ssh-9.0` 8→9; connector remains State 2 (`shared_service_account` inline loader live).

### Fixed — vmware `datastore.usage` composite null capacity on partial-detail builds

- **`vmware.composite.datastore.usage` now sources `capacity`/`free_space` from the listing row when the per-datastore detail omits them** (#2175): on some vCenter builds (observed 8.0.3 against the 9.0 spec) `Datastore.Info` populated `free_space` but not `capacity`, so the composite surfaced `capacity=null` and `%`-full was uncomputable off the composite alone. The row-builder now falls back to the already-fetched `GET:/vcenter/datastore` listing row (`entry.get(...)`) — detail still wins when present, `null` only when neither source carries it. No schema change (`capacity`/`free_space` already typed `["integer","null"]`); `vmware-rest-9.0` stays State 2 (`shared_service_account` live).

### Fixed — retire the registered-stub-twin vcf-logs orphan (migration 0052)

- **Data-only Alembic migration `0052` retires the stale long-product `vcf-logs` ingest orphan that migration `0049` missed** (#2185): `0049`'s per-op `EXISTS` twin probe matched nothing when the short (`vrli`) representation is a 0-row v2 class-registry stub at a divergent version (`9.0` vs the orphan's `9.0.2`), so the 136-op orphan survived every upgrade (the #2068 finding / #1910 review-shadow dead weight). `0052`'s predicate keys on the orphan's **own** attributes (`tenant_id IS NULL` + a `_PRODUCT_SPLITS` long product + the `parse_connector_id` `impl_id` rule) with no DB twin probe — safe because each split's short product is a registered connector class. Preserves `0049`'s guards (tenant rows untouched; non-split `impl_id` families left alone; `endpoint_descriptor` + `operation_group` retired together; `downgrade()` a documented no-op).

### Fixed — assert enabled gh composite L2 backings at connector load

- **`gh-rest-3` now asserts every enabled composite dispatching raw L2 sub-ops has a matching backing at connector load, raising `UnbackedEnabledCompositeError` otherwise** (#2186): `gh.composite.pr_status_summary` ships enabled but its three raw-REST L2 sub-ops only land after `meho connector ingest --catalog gh/3`, so a fresh-deploy dispatch trips `composite_l2_missing`. #1757 already surfaces the op as `unbacked` with a catalog `next_step` and returns a structured dispatch error, but nothing stopped a future composite from shipping enabled with no backing at all. The new `_register_and_assert_composite_backings` check reads the spec's own `sub_op_ids` (asserting the wiring, not DB state), so the inconsistency is non-regressible without crashing fresh deploys where the L2 ops are legitimately absent until ingest. The composite stays listed-but-unbacked (`is_enabled` unchanged) — this is a safety net, not a wire-up.

### Fixed — reconcile `/review` op count + structured not-ingested 404

- **`GET /connectors/{id}/review` now reconciles its op count with the listing and returns a structured `connector_not_ingested` 404 for registered-but-not-ingested connectors** (#2134): review's `total_op_count` summed only ops in rendered groups while `GET /connectors` counts every `EndpointDescriptor` row, so they disagreed by the ungrouped-op count (a consumer saw 136 vs 135). Adds an **additive** `ungrouped_op_count` (`total_op_count` semantics unchanged, so existing consumers are unaffected) such that `total_op_count + ungrouped_op_count` reconciles exactly to the listing's `operation_count`. Separately, `/review` returned a bare-string 404 for a registered built-in with 0 ingested ops (e.g. `vrli-rest-9.0`); it now raises the same structured `ConnectorNotIngestedError` (shared `connector_not_ingested_404` mapper) that `/operations/groups` returns, while unknown / tenant-curated / cross-tenant ids keep the deliberate plain-string 404. CLI OpenAPI snapshot regenerated for the new field.

### Fixed — fold a relative OpenAPI `servers[].url` base into ingested op paths

- **An ingested connector whose spec declares a relative `servers[].url`
  base (e.g. vRLI's `servers: [{url: "/api/v2"}]`) now dispatches to the
  full base-prefixed path instead of 404ing** (#2075, closes #1796):
  ingested dispatch built the request URL as `host:port + op_path`,
  dropping the server base, so an op resolved to `host:port/<version>`
  rather than `host:port/api/v2/<version>`. Per the reporter's
  lowest-blast-radius option, `_server_base_path` extracts and normalises
  the first server's relative URL and `_fold_server_base` joins it onto
  each op's `path` / `op_id` **at ingest time** (collapsing the double
  slash a trailing-slash base produces) — so dispatch is unchanged (no new
  descriptor column, no dispatch-path edit). **Absolute** server bases
  (`https://other-host/...`) stay out of scope: MEHO dispatches against
  the operator-configured target host, never a spec-declared one (the
  SSRF / topology boundary).

### Security — holodeck approval-gated remediation writes (G3.18-T2)

- **Three tightly-bounded remediation write ops added to `holodeck-ssh` behind four-eyes approval** (#2169): `holodeck.k8s.pods.gc` (delete Failed/Succeeded pods only), `holodeck.backups.prune` (keep newest-N under `/var/backups/**`), and `holodeck.images.import` (`ctr images import` restricted to `/root/containerd-images/*.tar`), all `safety_level="dangerous"` + `requires_approval=True`. A USER dispatch parks at `needs-approval` via the dispatcher `policy_gate`; handler bodies run only on the `_approved=True` resume path, and the `DISPATCH` audit row is written on execute. Each handler validates inputs against a fixed allowlist and rejects before composing the command, then `shlex.quote`s every interpolated token. Op count for `holodeck-ssh-9.0` 8→11. Retires the un-audited hand-run root-SSH recovery path that fixed the #2145 backup-fill outage.

### Security — harbor `robot.create` gated behind four-eyes approval

- **`harbor.robot.create` flipped to `requires_approval=True` so credential minting parks for a second operator** (#2173): the op mints a robot credential (`credential_mint`-classified) but shipped `requires_approval=False`, and the non-agent policy gate (`_non_agent_verdict`) keys the verdict solely on `requires_approval` — so a human `tenant_admin` could mint a Harbor robot credential with no second-operator approval. It now parks at `awaiting_approval` until approved via `POST /api/v1/approvals/{id}/decide`. `harbor.robot.delete` stays ungated (a recoverable `caution`-class access revoke, mirroring the bind9 precedent). Adds a no-bypass invariant asserting every `credential_mint` harbor op is approval-required.

### Security — bind9 dangerous config-apply ops require approval

- **`bind9.config.apply_file` and `bind9.config.apply_views` flipped to `requires_approval=True`, closing a non-agent four-eyes bypass** (#2126): both perform full DNS-config / view-tree replacement and shipped `safety_level="dangerous"` + `requires_approval=False`. Because the non-agent policy gate keys only on `requires_approval` (not `safety_level`), a human `tenant_admin` got `AUTO_EXECUTE` and could overwrite live DNS with no four-eyes step (the agent path already floored `dangerous`→needs-approval via `_SAFETY_CEILING`). Both now route through park→approve→resume for all principals; adds a `test_no_dangerous_op_bypasses_approval` invariant so a future bind9 write op can't reintroduce the gap.

### Added — the policy-gate verdict is stamped on the audit row

- **Every governed `call_operation` audit row now carries its `PermissionVerdict`** (#2129): a new nullable, CHECK-constrained `audit_log.policy_decision` column (migration `0051`) records `auto-execute` / `needs-approval` / `deny`, populated from a dispatch-scoped `policy_decision_var` contextvar the dispatcher binds right after the gate decides — the same cross-cutting seam already used for `run_id` / `work_ref` / `agent_session_id`, so both the dispatch-row writer and the approval-queue writer are covered by one change (the parked `approval.request` row correctly carries `needs-approval`). Surfaced on `AuditEntry`, so `POST /api/v1/audit/query` and `GET /audit/my-recent` support `WHERE policy_decision = '<verdict>'` with no join or `payload` parse. Rows where no gate ran (pre-gate usage errors, the approval-decision row, system-internal writers) stay `NULL`. OpenAPI snapshot regenerated.

### Added — `search_docs` carries an out-of-corpus grounding signal

- **`SearchDocsResponse` and the MCP `search_docs` tool payload now include a server-computed `grounded: bool`** (#2131): a caller can finally distinguish "the corpus has no answer" from a real hit and honor the doc-search contract (empty/not-grounded → do not silently fall back to training data). The verdict is presence-based and deterministic (`grounded ⇔ ≥1 chunk` — no model call, no read of the opaque corpus `score`), computed by a new shared `retrieval_is_grounded()` seam extracted from `ask_docs`'s empty-evidence check, so both surfaces share one verdict with no divergent threshold. OpenAPI snapshot regenerated.

### Added — `meho secret read` pipe-only raw-field emergency path

- **New `meho secret read --target <vault> <mount> <path> --field <f>` CLI verb** (#2224): the pipe-only emergency credential path writes **only the raw field value** to stdout — no key, envelope, quoting, or trailing newline — so `$(meho secret read …)` or a piped sshpass-class consumer gets exactly the credential bytes. It dispatches the existing audited `vault.kv.read` op (connector `vault-1.x`), extracting `--field` client-side, so the read rides the standard `/api/v1/operations/call` audit row (closing the un-audited local-Vault-read incident gap). Guardrails: refuses before dispatching if stdout is a real terminal (`stdoutIsTTY` seam), routes all errors to stderr, and leaves stdout empty on any failure so a consumer never eats an error string as a secret. CLI only — no new REST route or OpenAPI change.

### Fixed — doc citations no longer leak the storage backend

- **`search_docs` / `ask_docs` citation `source_url` is now backend-agnostic** (#2128): citations carried the corpus's raw object path (`gs://meho-knowledge-vmware-corpus/docs/...`), leaking the storage backend identity plus the internal bucket name and directory layout — a breach of the `doc-corpus` "the agent never sees the backend" contract. A new `normalize_source_ref()` in the shared `citation_links` seam is applied in `_project_chunk` (the single place every `DocsChunk` is born, so response chunks, ask_docs citations, the MCP wire, and the synthesis prompt are all covered): a KB / already-`https` source resolves to its canonical public URL, and a community/unrecognized `gs://` or missing source becomes an opaque `meho://docs/<collection>/<chunk_id>` ref. The raw `gs://` path is never emitted; the corpus adapter's `CorpusChunk.source_url` stays raw (normalization is applied at MEHO's projection boundary).

### Fixed — `meho docs search` applies the same gate as REST `search_docs`

- **The CLI `docs` tree no longer keys visibility on a client-side capability pre-check** (#2182): `meho docs search` decoded the stored JWT for a bare-`meho-docs` capability and hid the whole `docs` tree with `addon_not_provisioned` (exit 5) before any network call, while the REST route only enforces the per-collection `meho-docs:<collection>` entitlement server-side — so a tenant entitled via REST could still be refused on the CLI. Per the recorded #2109 decision (reconcile to one server-gated op), the client-side pre-check is removed entirely (`Capability` / `tenantHasDocsCapability` / `capabilitiesFromJWT` / `loadStoredToken` / `errNotProvisioned` and the `provisioned` plumbing deleted): the tree is always visible, every verb defers to the backplane, and a per-collection entitlement miss surfaces as the route's `not_entitled` 403, which the CLI already renders as `insufficient_role` (exit 5). The separate MCP-tool `required_capability="meho-docs"` gate is unchanged. CLI only.

### Fixed — JSONFlux serializes the inline sample once, bounded by bytes

- **The JSONFlux inline sample is now serialized exactly once and byte-bounded** (#134): the reducer serialized a duplicate summary copy of the sample and had no serialized-size ceiling, so object-heavy list ops could overflow the MCP result ceiling. The summary duplicate is dropped (keeping `handle.sample_rows`, whose length still feeds the audit `sample_rows_returned` field) and the inline sample is capped by a new named `jsonflux_sample_byte_budget` setting (default 4096); the `result_query` recovery path is unchanged.

### Fixed — dict-of-arrays detail ops skip JSONFlux list reduction

- **JSONFlux no longer collapses a single-object detail op into one of its sibling arrays** (#2184): for a flat detail object like `k8s.pod.info`, whose sibling arrays (`containers` / `container_statuses` / `volumes` / `conditions`) are coordinate fields of one object, the reducer's largest-list fallback would — once the projection cleared the byte threshold — materialize the longest array (`conditions`) into a handle and silently drop every sibling, including the `container_statuses` an operator actually reads. `_detect_collection` in `jsonflux_reducer.py` now treats a keyless dict as a paginable collection only when it has **exactly one** list-valued field (the `k8s.logs` shape); a dict with **more than one** list-valued field is a dict-of-arrays detail object and passes through verbatim, so no sub-array is ever selected as the collection. This is the structural fix (one collection = a list; several coordinate arrays = an object) rather than a k8s-specific ranking band-aid, and `k8s.logs` still reduces via the single-list branch.

### Added — discard verb for unpublished draft runbook templates

- **A `discard` leg completes the runbook template CRUD lifecycle, deleting a `(tenant, slug, version)` row only when `status == "draft"`** (#2127): a draft (typo'd slug, wrong steps) previously had no removal path but the publish-then-deprecate workaround, since `/deprecate` refuses a draft. Added across both operator surfaces on one backplane — `RunbookTemplateService.discard()` (published/deprecated versions raise `TemplateNotDraftError` pointing at `deprecate` so lifecycle history is never erased; a missing triple raises `TemplateNotFoundError`), REST `POST /api/v1/runbooks/templates/{slug}/discard` (`tenant_admin` only, `audit_op_id="runbook.discard_template"`), and MCP `meho.runbook.discard_template` sharing the service method. `POST /{slug}/discard` mirrors the `publish`/`deprecate` verbs and avoids DELETE-with-body's undefined semantics. OpenAPI snapshot + generated client regenerated; runbook tool-count invariant bumped eleven→twelve.

### Fixed — runbooks-UI abort 403 is CSRF, not RBAC/opacity

- **The `/ui/runbooks/runs/<id>/abort` `403` vs `/api/v1/.../abort` `200` differential is now documented as a CSRF rejection, and the client surfaces it** (#2183): the `/ui` abort route sits on the same operator floor as REST (`require_ui_session`, `caller_is_admin=probe.is_tenant_admin`) — a genuine assignee denial there renders an inline HTTP-200 fragment, never a 403 — so the only 403 on a `/ui/*` POST is the double-submit-cookie middleware (raw curl carries a Bearer JWT but no `meho_csrf` cookie; `/api/v1/*` is CSRF-exempt). `docs/codebase/ui.md` + the driver docstring gain a CSRF-requirement section; `session-expiry.js` branches on `x-csrf-rejection-reason` (htmx 2.0.9 won't swap 4xx bodies, so the client is the only surface) to show a "token expired — refresh and retry" banner, leaving a bare RBAC 403 untouched; and `_render_detail` threads `admin_lift_degraded` so a `tenant_admin` whose role lift silently soft-failed (JWKS/session hiccup → `operator is None`) gets a diagnostic + retry link instead of being dropped onto the restricted opacity banner. Docs + template + tests only; OpenAPI snapshot byte-identical.

### Fixed — expired runbooks sessions re-auth instead of the opacity banner

- **The runbooks detail read surface now re-raises the terminal `session_expired` 401 from `_resolve_role` instead of swallowing it to a degraded operator** (#2120): the surface recomputes the admin verdict per request by re-verifying the session's stored access token, but a broad `except` degraded a genuine `tenant_admin` whose token had aged out onto the opacity-restricted banner ("complete a run to unlock") — browser-dependent (tokens expire independently) and locking template authors out of their own drafts. The terminal expiry now propagates to the app-level handler (`ui/auth/errors.py`, G0.25 #1694), which 302-redirects the browser to `/ui/auth/login?return_to=…`; after re-login the fresh token lifts the real role and full steps render. Transient / malformed-token lifts still fail soft to the restricted view, preserving the anti-enumeration posture and the never-5xx guarantee.

### Fixed — expired sessions re-auth in the connectors `resolve_role_probe`

- **`resolve_role_probe` (`ui/routes/connectors/operator.py`) now re-raises the terminal `session_expired` 401 instead of returning a no-privileges probe on any failure** (#2121): the sibling of #2120 on the connectors surface — a broad `except Exception` swallowed the terminal expiry, silently degrading a `tenant_admin` whose token aged out to a no-privileges render on both the connectors detail page (`GET /ui/connectors/{name}`) and the runbooks run-driver POSTs (`driver.py` / `driver_render.py`) that read `caller_is_admin` from this probe. The terminal expiry now propagates to the app-level handler for a re-login redirect; transient / malformed-token failures still fail soft to the no-privileges probe so the read surface never 5xxes. The strict `resolve_operator_or_403` write gate (already propagating) is unchanged.

### Fixed — runbooks authoring surface (preview, draft edit, empty body)

- **Three defects on the `/ui/runbooks` authoring surface are repaired** (#2122): (D1) the step-body live preview used the shadow-DOM piercing combinator `hx-target="closest .flex-wrap >> .runbook-step-preview"`, invalid in htmx target resolution / `Element.closest()`, so Preview threw a `SyntaxError` and never fired — retargeted to `next .runbook-step-preview`; (D2) a draft detail page offered only Publish, forcing authors to publish a possibly-broken first cut to reach an Edit button — added an in-place Edit link to the shared `/ui/runbooks/<slug>/edit` editor (the engine's `update_or_fork` mutates a draft in place, leaving no vestigial version); (D3) empty `ManualStep.body` / `OperationCallStep.body` were plain `str` and `""` passed — added `min_length=1` so an empty body is a 422 at request validation across both REST and MCP via the shared schema.

### Fixed — runbook editor writes bind a canonical audit op_id

- **The UI editor write path (`handle_editor_submit`, `POST /ui/runbooks/new` + `POST /ui/runbooks/<slug>/edit`) now binds `audit_op_id` (`runbook.draft_template` on create, `runbook.edit_template` on edit) + `audit_op_class="write"` + `audit_slug` before dispatch** (#2124): previously a UI-authored template write was audited under a path-derived / `ui.view.*` op id, so an op-id-scoped audit query for `runbook.*` missed it. The chassis `AuditMiddleware` now records the row under the canonical op id, mirroring the REST `POST`/`PATCH` and run-driver routes. REST-path audit (already correct) and the target-scoped `who-touched` / payload-nested `my-recent` query surfaces were confirmed working-as-designed and left unchanged.

### Fixed — empty runbooks list `target_kind` filter no longer blanks the catalog

- **The `/ui/runbooks` + `/ui/runbooks/list` `target_kind` query param now coerces empty → `None` (the `EMPTY_STR_TO_NONE` the `status` param already had)** (#2125): an empty `?target_kind=` reached `RunbookTemplateService.list_templates` as `""` and applied `WHERE target_kind = ''`, matching nothing — so the post-deprecate `runbooks-refresh` (which re-fetches `/ui/runbooks/list` with an empty target-kind input in `hx-include`) emptied the whole catalog after every deprecate. The `max_length` guard moves onto the inner `str` branch via `StringConstraints` so it isn't applied to the coerced `None`.

### Fixed — byte-identical runbook template edit skips the no-op fork

- **`update_or_fork` now dedups a byte-identical edit of a published template instead of minting a redundant `v=max+1` draft** (#2132): published versions are immutable so an edit forks a draft, but with no dedup an accidental Edit → Submit of the pre-populated UI form created a draft differing only in metadata, polluting version history. A new `_body_matches` helper compares the four author-supplied body fields (title / description / target_kind / steps, in storage shape) against the source; on a match it skips the fork and returns the unchanged source (`forked_from=None`). Engine-side seam, so both the UI editor and REST `PATCH` inherit the dedup. `EditTemplateResponse.status` widened from `Literal["draft"]` to `Literal["draft","published","deprecated"]` (additive to the wire) so the no-op return honestly reports the unchanged source's status; the UI keys on `forked_from`, so no consumer breaks. CLI snapshot + docs updated.

### Fixed — htmx-process Alpine-cloned editor steps so step 2+ previews fire

- **The runbook authoring editor now hands the form root to `htmx.process` on new step subtrees, so per-step live previews fire beyond the first step** (#2225): each step's Body textarea carries `hx-post="/ui/runbooks/preview"`, but the step markup lives inside an Alpine `<template x-for>` loop — htmx only binds `hx-*` on DOM it renders itself, so Alpine-cloned steps (via **+ Add step**, or any step past the first on an edit) were never htmx-processed and issued no preview POST. A `processSteps()` helper (idempotent — htmx skips already-initialised nodes) folds into a shared `rescan()` run on first render, every `htmx:afterSettle`, and after each add/remove click. Also scoped the form's inherited `hx-disabled-elt` with `hx-disinherit="hx-disabled-elt"` — it had leaked `find button[type=submit]` into each descendant preview POST, matching nothing and logging a warning once per preview — keeping the value on the form's own submit.

### Added — global htmx 401 session-expiry safety net

- **Background htmx requests now surface a mid-session token expiry instead of dying silently** (#2055): a single global `htmx:beforeOnLoad` listener on `<body>` (`session-expiry.js`, loaded from `_head_assets.html` after `htmx.min.js`) intercepts every response before swap and, **only** on `xhr.status === 401`, `preventDefault()`s the dead swap and reveals a fixed recovery banner linking to `/ui/auth/login?return_to=<current path>` (matching the server handler's `quote(full_path)` contract). Previously htmx 2.0.9's default `[45]..` handling swallowed the JSON 401 on `Accept: */*` XHRs — the server redirect (#1694) only fires on full-page `Accept: text/html` navigations — leaving the operator with a dead control and no signal. Every non-401 status (2xx swap, 422 form re-render, 403 CSRF) returns early untouched; client-side app-shell JS only, no route or OpenAPI change.

### Fixed — token-consuming /ui/* lifts route through the refresh-on-401 seam

- **Nine per-route operator lifts that re-verified the stored access token with a bare `load_session` + `verify_jwt_for_audience` now go through `load_fresh_session` + `verify_access_token_with_refresh`** (#2056): an expired-but-refreshable token used to 401 mid-session even though the session cookie and refresh token were still valid (the `ui-memory-create-401-token-expired-no-refresh` signal). Every token-consuming `/ui/*` lift now shares the single RFC 9700 refresh chokepoint (`ui/auth/refresh.py`, #1694) — mirroring the `approvals` / `corpus` / `retrieval` precedent — refreshing once and failing soft to the documented `session_expired` terminal state instead of a bare 401. Server-rendered Jinja / BFF only; no OpenAPI change.

### Fixed — expired OAuth-callback-state raw-JSON dead-end

- **An expired OAuth callback `state` on `GET /ui/auth/callback` no longer dead-ends on a raw-JSON `400 {"detail":"authorization_failed"}` in the browser** (#2176): the recoverable branch of `_exchange_or_translate` (expired/mismatched `state`, verifier-store miss, `MismatchingStateError`) now raises a distinct `authorization_state_expired` detail, and the `ui.auth.errors` handler intercepts it on the callback path to return a `303 /ui/auth/login` one-click restart for HTML navigations (no `return_to`, no cookie touched — it's pre-session) while scripted callers keep the structured body. The genuine IdP `?error=` decline (`authorization_failed`) and the token-endpoint `502` stay distinguishable and are not collapsed into "start over". Server-rendered Jinja / BFF only; no OpenAPI change.

### Fixed — empty topology kind/q filters coerced to no-filter

- **The `/ui/topology` "All kinds" option and name search now return rows again** (#2133): the `kind` filter treated an empty string as an exact-match value (`WHERE kind = ''`), returning zero rows — and because the filter form co-submits `kind` on every search keystroke (`hx-include="closest form"`), name search always rode with `kind=` and returned nothing too. Coerces `"" -> None` on `kind` (and defensively `q`) via the shared `EMPTY_STR_TO_NONE` `BeforeValidator` — the pattern #2125 applied to the runbooks `target_kind` filter — across both the table and graph branches that share the params, with `max_length` moved onto the inner `str` branch via `StringConstraints` so it isn't applied to the produced `None`. Server-rendered Jinja query params only; no OpenAPI change.

### Fixed — targeted 422 hint for cross-surface memory write fields

- **`POST /api/v1/memory` now names the correct REST field when an MCP-surface field name is sent** (#2147): a Pydantic v2 `@model_validator(mode="before")` on `RememberBody` runs ahead of extra-field rejection and raises a targeted `ValueError` for exactly `ttl` (message names `expires_at`, ISO-8601) and top-level `tags` (message names `metadata.tags`), instead of a generic `extra_forbidden` 422 that named no correct field. Any other unknown field still gets the standard `extra_forbidden`; accepted shapes, defaults, and semantics are unchanged, and the intentional MCP↔REST split (`mcp/tools/memory.py` translates `ttl`/`tags` by design) stays. Validation-message only; no OpenAPI change.

### Fixed — topology table sort-direction toggle re-renders the `<thead>`

- **The topology table sort-direction toggle is no longer frozen** (#140): the HTMX swap replaced only `<tbody id="topology-table-body">`, leaving the `<thead>` (server-computed `next_dir` links + active-column arrow) stale. The sortable head is extracted into a shared partial (`_table_head.html`) that `table.html` includes in place for the full page and `_table_rows.html` re-emits out-of-band (`hx-swap-oob`, wrapped in a `<template>` per the htmx 2.0.9 table-element OOB caveat) on every swap. Route change is context-only (`is_fragment`/`oob` flag); server-rendered Jinja only, OpenAPI snapshot unaffected.

### Fixed — topology node drawer renders beside the table, not off-screen

- **The topology node-detail drawer no longer stacks ~1300px below the fold** (#141): the table (and graph) and the drawer are wrapped in a shared `lg:grid` so the drawer renders beside them, with an `hx-on::after-swap` `scrollIntoView` for the narrow/stacked case, and `self-start` on both the placeholder and the swapped-in fragment roots (`_drawer.html`, `_drawer_not_found.html`) so found and not-found states render as top-aligned cards. Server-rendered Jinja template + CSS only; no route or OpenAPI change.

### Fixed — topology graph `?selected` cross-link selects/centers/opens the node

- **Arriving at `?view=graph&selected=<id>` now selects, centers, and opens the node** (#142): the `layoutstop` cross-link handler is registered before the initial Cytoscape layout runs — the graph is constructed without the inline `layout:` option, the tap/layout-switch/cross-link listeners are attached, then `cy.layout(layoutOptions("cose-bilkent", false)).run()` is called. With `animate:false` cose-bilkent completing layout synchronously in-tick, the old constructor-`layout:` ordering emitted `layoutstop` before the `cy.one("layoutstop")` listener attached, so the cross-link never fired. Client-side topology-graph JS only; no route or OpenAPI change.

### Security — enforce JWT `exp` as an essential claim

- **A JWT that omits `exp` is now rejected with `401 missing_exp`** (#2057) instead of being accepted as a non-expiring token. `_decode_with_jwks` now marks `exp` `{"essential": True}` in its `claims_options`, so authlib's `claims.validate()` turns an absent `exp` into a `MissingClaimError` (previously it only checked expiry *when `exp` was present*, so an `exp`-less token decoded cleanly and the route returned 200). `_classify_missing_claim` maps that to the specific `missing_exp` 401 code, mirroring the existing `missing_sub` handling, per RFC 9068 §2.2.1 which makes `exp` REQUIRED on access tokens. A present-but-malformed `exp` already failed closed via authlib (`invalid_claim`); tokens that omit the claim are the only newly-rejected case. Realms must ensure their access tokens carry `exp`. No schema change.

### Security — tenant-scope `count_known_ops` to close a cross-tenant oracle

- **The `known_op_count` returned in the `unknown_op` error payload is now scoped to the caller's tenant** (#2058), closing a cross-tenant op-count / connector-existence oracle on the dispatch error path. `count_known_ops` previously counted enabled `endpoint_descriptor` rows by `(product, version, impl_id)` with no tenant predicate, so a tenant-A caller probing a connector private to tenant B learned its enabled-op count (info disclosure, IDOR-adjacent). The fix mirrors the in-repo `connector_exists()` precedent — `(tenant_id IS NULL) OR (tenant_id == caller)` — and threads the caller's tenant through every call site: `dispatch` / `preview_dispatch` pass `operator.tenant_id`, while the operator-less typed-connector chassis shims (holodeck / pfsense / kubernetes / bind9) pass `tenant_id=None` (collapsing to global rows only, matching their own descriptor query). No schema change.

### Security — confine KB ingest to `KB_INGEST_ROOT` (path-traversal / LFI)

- **The server-side KB bulk-ingest surface (`POST /api/v1/kb/ingest`, `meho kb ingest`) is now confined to a pinned `KB_INGEST_ROOT` directory** (default `/opt/meho/kb-ingest`) (#2059), closing a path-traversal / local-file-inclusion hole where a `tenant_admin` could ingest any `.md` anywhere on the backplane host into their corpus. The new `_resolve_within_ingest_root` helper resolves both the root and the requested directory (`Path.resolve` follows symlinks and collapses `..`) and raises `KbIngestRootError` unless the resolved path lands inside the root — rejecting both `..` traversal payloads and escaping symlinks before any file is read, including in dry-run mode. The route maps the error to a structured `400 kb_ingest_path_outside_root`, matching the existing `directory_not_found` / `not_a_directory` shape. **Deploy impact:** the Helm chart surfaces the root as `config.kbIngestRoot` → `KB_INGEST_ROOT` (configmap + values + schema), but does not pre-create or mount it — operators that ingest must mount their KB content under the root or repoint `KB_INGEST_ROOT`; a chassis-only deploy that never ingests carries the default harmlessly. New `settings.kb_ingest_root` field; no API schema change (OpenAPI snapshot unchanged).

### Security — clickjacking-defence headers on `/ui/*`

- **Every `/ui/*` response now carries `Content-Security-Policy: frame-ancestors 'none'` and `X-Frame-Options: DENY`** (#2060), so the operator console can no longer be loaded in an `<iframe>` for a clickjacking attack (previously it shipped no framing protection at all). A new pure-ASGI `UIFramingHeadersMiddleware` (`ui/security_headers.py`) stamps both headers (CSP Level 2 plus the legacy `X-Frame-Options` fallback, per the OWASP Clickjacking Defense cheat sheet) and is registered **outermost** in `main.py` so they land even on the 302-to-login the inner session middleware short-circuits on (a framed login page is itself a clickjacking surface). It is `/ui/`-prefix-scoped, so `/api/*` and `/mcp` JSON responses pass through unstamped, and the CSP is deliberately `frame-ancestors`-only so it cannot break the console's HTMX + Alpine inline-script render; `setdefault` preserves any route that sets its own `frame-ancestors`. Middleware-only change — OpenAPI snapshot regenerated with no diff.

### Security — sandbox the JSONFlux DuckDB query engine

- **The JSONFlux `QueryEngine`'s in-memory DuckDB connection is now hardened at construction** (#2061), closing a latent arbitrary-file-read / SSRF / untrusted-extension sink. Data only ever enters the engine via in-memory Arrow tables (`conn.register`) — DuckDB never reads a file or URL itself — but the connection previously opened with no config restrictions, so arbitrary SQL (e.g. a future `result_query` drill-in) could reach external access. `_harden_connection` now runs `SET enable_external_access=false`, `SET allow_community_extensions=false`, then `SET lock_configuration=true` **last** (it freezes all further config, so the preceding `SET`s must precede it), per the DuckDB securing guide. No legitimate path relied on DuckDB external access, so the jsonflux/query suite passes unchanged. Backend-internal change; no schema change.

### Security — pin the pooled httpx client to same-origin redirects only

- **The shared per-target pooled `httpx.AsyncClient` no longer follows redirects across origins** (#2062), closing an open-redirect SSRF / auth-token-forwarding hole. The client defaulted to `follow_redirects=True`, so a `3xx` from a vendor session-create/login `POST` was transparently replayed to its `Location`; httpx strips `Authorization` cross-origin but **not** vendor auth headers (NSX's `X-XSRF-TOKEN`) nor — on a method-preserving `307`/`308` — the credential request body, so an off-origin redirect could harvest the login secret and session token. The new `_SameOriginRedirectClient` is built `follow_redirects=False` and re-implements a bounded, same-origin-only redirect loop in `send()`: a benign same-origin trailing-slash `301` (vcsim's `/rest` mount, some appliances) is still followed so no vendor flow regresses, but a cross-origin `Location` is refused and the unfollowed `3xx` returned — the request is never replayed off-origin, so no header or body crosses the origin boundary. Centralised at the one pooled-construction point, so every HTTP connector (NSX, VCF/vmware session-login) is covered with no per-sink edit. No schema change.

### Security — require HTTPS for the backplane URL

- **The `meho` CLI now enforces a transport-security policy on the backplane URL** (#2063), preventing the `meho login` bearer token from being transmitted in cleartext. Both `meho login` and the shared resolver previously accepted any scheme, so a plaintext `http://` backplane sent the token in the clear on every request. `backplane.NormaliseURL` now accepts `https://` always, accepts plaintext `http://` only for a loopback host (`localhost` / `127.0.0.0/8` / `::1`), and rejects `http://` to any routed host. `meho login` is stricter at first contact — it normalises via `NormaliseURLAllowHTTP(arg, allowHTTP=false)` (https-only even for loopback) and adds an `--insecure-allow-http` opt-in (mirroring the existing `--insecure-skip-tls-verify` flag) that permits a localhost plaintext backplane for local dev; the flag never permits plaintext to a routed host. A bare host with no scheme is rejected as a parse error (no scheme is silently assumed). Go-only change; no `cli/api/` regeneration needed.

### Security — reject `${...}` substitution in runbook `op_id` at publish

- **A runbook step's or verify's `op_id` may no longer contain a `${...}` substitution token** (#2064), closing a parameter-injection-into-operation-identity hole. The publish-time substitution validator walked step `body`, op-call `params`, verify `params`, and verify `expect`, but skipped both `op_id` fields — so a `${...}` token in `op_id` reached storage unchecked and was substituted at run time (`run_service.py` `_substitute_string`), letting an operator-supplied run parameter redirect a published step/verify to a *different* operation. The new `validate_op_id_static` helper holds `op_id` to a stricter rule than the other fields — operation identity must be static, so **no** substitution is legal there, not even an allowlisted `${run.target}` / `${run.params.X}` — and is wired into the template-body validator. Such a template is now refused at publish (`422`, `disallowed substitution in op_id: …`) instead of silently substituted at dispatch; the runtime substitution is left in place as harmless defence-in-depth (a static `op_id` passes through unchanged). OpenAPI snapshot regenerated with no diff.

### Changed — FastAPI 0.137 adopted

- **The backend now requires FastAPI `>=0.137.1,<0.138`** (#2054, closes #1819): the deliberate `>=0.136.3,<0.137` hold (0.137 nested routes out of `app.routes` and leaked a handler-500 at `TestClient` teardown) is lifted now that the test suite is adapted to the 0.137 router-tree and exception internals — the OpenAPI-paths assertions walk the new nested router shape and the TestClient teardown uses `ASGITransport`. Dependency pin + test-compat only; no runtime behavior change.

### Changed — CI supply-chain + gate hardening

- **The `helm install + helm test` job is now a required check** (#2052, closes #349): its `continue-on-error` escape hatch is removed so a broken chart smoke fails the build instead of passing silently.
- **Database-migration tests run only when migration files change** (#2140): the alembic/migration suite is relocated to `backend/tests/migrations/`, excluded from the unit lane, and driven by a dedicated conditional `python-migration-tests` CI job gated on a `changes`-detection step — so the heavy migration suite no longer runs on every PR, only when `backend/alembic/**`, `backend/tests/migrations/**`, or the migration tooling changes.
- **The Python dependency license gate now allows `MIT-0`** (#2197): the MIT-0 (No Attribution) license is added to the allowlist so a compliant transitive dependency no longer red-flags the license check.

### Changed — dependency bumps

- Bumped: `actions/setup-python` 6.2.0→6.3.0 (#2095), `azure/setup-helm` 5.0.0→5.0.1 (#2097), `actions/setup-go` 6.4.0→6.5.0 (#2098), `redis` 8.0.0→8.0.1 (#2100), `google-auth` 2.55.0→2.55.1 (#2102), `ruff` 0.15.18→0.15.20 (#2103), `alembic` 1.18.4→1.18.5 (#2104), `presidio-analyzer` 2.2.362→2.2.363 (#2105), `asyncssh` 2.23.1→2.24.0 (#2106), and `joserfc` (#2118, #2141).

## [0.19.0] - 2026-06-22

### Fixed — profiled vCenter legacy session-token shape

- **Profiled `session_login_basic` now accepts vCenter's legacy `{"value": "<token>"}` session-token object shape** (#1964 / #2047): #2031 gave the profiled vCenter scheme a modern→legacy session **login-path** fallback (POST `/api/session`, on HTTP 404 retry `/rest/com/vmware/cis/session`), but the profiled **token extractor** was left modern-only — it read the token **only** as a raw JSON string body. The legacy vCenter session endpoint returns the token nested as `{"value": "<token>"}`, so on a genuinely legacy-only vCenter (or a vcsim build serving the legacy shape) the login-path fallback succeeded but token extraction then failed and the harness raised the target-named auth error — the fallback was only half-wired. `_extract_session_login_basic_token` now reads both shapes, mirroring the typed `VmwareRestConnector._extract_session_token`: a non-empty `str` body → token (unchanged), and a `{"value": <non-empty str>}` object body → token (new). Anything else (empty string, empty/missing/non-string `value`, list, non-`str`/non-`dict`) still returns `None` so the harness raises the consistent target-named error. The object-shape key is hoisted to a single shared `SESSION_TOKEN_OBJECT_KEY` constant in `_shared/profile_auth.py` that the typed connector imports, so the two paths can't silently diverge. Connector internals + tests only — no FastAPI route/schema change, OpenAPI snapshot unchanged; `validate_shipped_artifacts()` still boot-validates.

### Added — shipped-spec provenance ADR

- **Derivative-work / interoperability provenance record for the MEHO-authored shipped specs** (#1964 T? #2036): the minimal OpenAPI specs + `ExecutionProfile`s shipped for `vmware/9.0` + `sddc/9.0` (#1976) reproduce vendor API path/parameter/field names verbatim so the dispatcher can address the real appliance; that derivative-work question was flagged by the #1966 initiative for a SECURITY/legal glance but never formally recorded. A new `docs/decisions/shipped-spec-provenance.md` ADR (mirroring `docs/decisions/jsonflux-license.md`) consolidates the provenance: what is copied (functional interface elements — path/param/field names) vs not (no vendor prose/examples/full surface; full vendor specs stay `upstream` pointers, not redistributed), the minimal-extent-necessary interoperability rationale, and the Apache-2.0 + evoila-copyright posture on the MEHO-authored files. It cross-links the per-file header rationale rather than duplicating it, and carries an explicit **"Signoff of record"** section left for a human (legal/maintainer) — the ADR records the facts and routes the decision, it does not assert a legal conclusion. `NOTICE` gains an entry pointing at the ADR. Docs/governance only — no code, spec, or profile content changes; SPDX headers unchanged.

### Fixed — connector spec-ingest wording

- **Stale `force-include` and "legacy fallback out of scope" wording in the spec-ingest area** (#1964 / #2035): two waves of comment/docstring/runtime-string drift were corrected, with **no behavior change** (comments, docstrings, and operator-facing advisory strings only). (1) ~8 sites described the shipped spec & profile resource dirs as `force-include`d in the wheel; the real mechanism is hatch's `[tool.hatch.build.targets.wheel].artifacts` (the dirs live inside the package tree, are collected by the `packages` glob, and are listed in `artifacts` for explicitness — deliberately **not** in `force-include`, which would trigger a duplicate-archive build failure). The two operator-facing `CatalogError` remediation strings (`load_spec_resource` / `load_profile_resource`) no longer tell an operator to "force-include" anything. The legitimate `alembic.ini` / `alembic` tree `force-include` references (`db/migrations.py`, `docs/codebase/backend.md`) are unchanged — that tree is genuinely outside the package. (2) The shipped `vmware_rest_minimal.yaml` spec + profile headers and the `_shared/profile_auth.py` docstrings said the profiled vCenter scheme was modern-`/api/session`-only with the legacy fallback out of scope; #2031 (PR #2033) shipped the modern→legacy `/rest/com/vmware/cis/session` 404 fallback + per-endpoint `/api`↔`/rest` op-path mount on the profiled path, so those now describe the shipped behavior. `validate_shipped_artifacts()` still passes (YAML header comments don't affect parsing). No FastAPI route/schema change; OpenAPI snapshot unchanged.

### Added — config-driven connectors (vCenter modern→legacy session fallback)

- **Profiled vCenter `session_login_basic` modern→legacy `/api/session` 404 fallback + per-endpoint op-path mount** (#1964 / #2031): the `session_login_basic` scheme #2025 added established a vCenter session against the **modern** `POST /api/session` only, so a **legacy-only** deployment (notably the upstream `vmware/vcsim` simulator, which registers only `POST /rest/com/vmware/cis/session`) could not authenticate through the profiled connector — and even if it could, ingested ops mounted at the wrong base path. The shared profiled session harness (`ProfiledRestConnector`) now mirrors the typed `VmwareRestConnector`: it POSTs the modern path first and, on **HTTP 404 only** (401/403/5xx are auth/server failures and are **not** retried on legacy), falls back to the legacy `/rest/com/vmware/cis/session` path. The winning login path is recorded per target and drives both **op-path mount** (`mount_op_path`: `/api` on modern, `/rest` on legacy/vcsim, so a legacy-only target stops defaulting to `/api` and 404ing every op) and session teardown — the same `_session_paths` shape the typed connector uses. The fallback is expressed as a **closed, vetted** `LegacyFallback` constant on the named scheme's `SessionSchemeSpec` (a single vetted vCenter modern/legacy pair, **not** a free-form list of candidate login paths on the profile / `AuthSpec` — the #1177/#1969 no-DSL line holds; `AuthSpec` gains no fields and `test_auth_spec_has_no_dsl_fields` stays green). Only `session_login_basic` declares a fallback; the vRLI `session_login` scheme is single-path and unaffected. `vmware_rest_minimal.yaml` still boot-validates; an integration test exercises the legacy fallback end-to-end against a respx-mocked legacy-only vCenter surface (mirroring how the typed connector is tested — vcsim doesn't serve the REST appliance API, evoila/meho#536). Connector internals only — no FastAPI route/schema change, OpenAPI snapshot unchanged.

### Added — config-driven connectors (vCenter session_login_basic scheme)

- **`session_login_basic` named auth scheme so the shipped vmware profile dispatches** (#1964 T? #2025): the shipped `vmware_rest_minimal.yaml` profile declared `scheme: session_login`, but that named extractor was hardcoded to the **vRLI** login shape (JSON `{username,password,provider}` body to `/api/v2/sessions`, token read from `.sessionId`, sent as `Bearer`), so a profiled vCenter would ingest and pass `validate_shipped_artifacts()` at boot yet **fail to authenticate at runtime** — defeating the executable-connector goal. A new **closed-catalog** named scheme `session_login_basic` captures the real vCenter shape, grounded in the typed `VmwareRestConnector`'s `POST /api/session` flow: HTTP **Basic** credentials on the login POST (no JSON creds body), the response body **is** the token as a raw JSON string, and the established token rides downstream requests verbatim in the bespoke `vmware-api-session-id` header (not `Bearer`/`Authorization`). It is added to the `AuthSchemeName` `Literal` / `NAMED_AUTH_SCHEMES` and validated like the others (no free-form `login_path`/body DSL on the profile — the #1177/#1969 closed-catalog line holds; every per-scheme knob lives on the vetted `SessionSchemeSpec`, not `AuthSpec`). The hoisted session harness (`ProfiledRestConnector`) grew two declarative per-scheme controls — `login_credentials` (`body` vs HTTP `basic` on the login POST) and `token_header` / `token_value_kind` (where + how the established token is applied) — so it drives both the vRLI and vCenter shapes from one audited lock / cache / single-flight / fail-closed implementation. The shipped `vmware_rest_minimal.yaml` is repointed to `session_login_basic` (still boot-validates); only the modern `/api/session` path is mirrored — the legacy `/rest/com/vmware/cis/session` fallback the typed connector also tries is out of scope (#2025).

### Added — config-driven connectors (shipped-spec on-ramp)

- **Catalog `spec_resource` / `profile_resource` shipped-artifact on-ramp** (G?·#1964 T1 #1975, the mechanism half of config-driven executable connectors): a catalog row may now carry MEHO-authored OpenAPI specs + `ExecutionProfile` documents as **package data** instead of relying on a fetchable `upstream`. New optional `spec_resource` / `profile_resource` fields on `ConnectorSpecEntry` name a single resource (path-traversal-rejected at parse time) under `meho_backplane.operations.ingest.specs` / `meho_backplane.connectors.profiles`; the catalog-driven `POST /api/v1/connectors/ingest` route loads the spec bytes via `importlib.resources` straight into `SpecSource.content` — bypassing the fetch + SSRF guard and the `catalog_entry_upstream_not_spec` / `catalog_entry_templated_upstream` 422s that block products whose upstream is an HTML developer portal (vmware/sddc) or an fqdn-templated appliance URL. The catalog **validator now exempts a profile-backed row** (carrying `profile_resource`) from the class-presence + triple-registration checks, since its synthesised `ProfiledRestConnector` subclass is materialised from the reviewed profile (#1971) and need not pre-exist at boot. Every shipped artifact is **dry-run-parsed at startup** with the same parser the live path uses (`parse_openapi` for specs; `ExecutionProfile` validation + `validate_execution_profile` for profiles) — a malformed shipped artifact crashes the lifespan (CI app-boot smoke) instead of 500-ing the first `--catalog` ingest. The two resource dirs ship in the wheel as package data (listed in `[tool.hatch.build.targets.wheel].artifacts`). T1 ships the mechanism plus a `_fixture/1.0` profile-backed row exercising the boot validator + ingest path end to end; T2 (#1976) authors the real vmware/sddc specs. The widened `ConnectorSpecEntry` regenerates the OpenAPI snapshot + Go client (#1975).

### Added — config-driven connectors (catalog shipped-spec CLI indicator)

- **`meho connector catalog list` `ships` column** (#1964 T? #1981): the human catalog table was blind to the `spec_resource` / `profile_resource` fields #1975 added to `ConnectorSpecEntry` — an operator scanning rows could not tell which (product, version) ships a MEHO-authored OpenAPI spec / `ExecutionProfile` as package data (no fetch / upload needed) versus one that still needs an upstream fetch or an explicit `--spec` upload. `printCatalogTable` now renders a `ships` column between `spec_ver` and `notes` whose marker distinguishes all four independent combinations: `spec+prof` (ships both), `spec` (local spec only), `prof` (local profile only), and `-` (neither — the upstream-fetch / upload on-ramp). The `--json` path is unchanged — it already carried the typed fields via `output.PrintJSON`. CLI-only; no backend route/schema touched, so the OpenAPI snapshot is unchanged.

### Added — config-driven connectors (vmware/sddc shipped specs)

- **MEHO-authored minimal specs + `ExecutionProfile`s for `vmware/9.0` + `sddc/9.0`** (#1964 T2 #1976, the artifact half of config-driven executable connectors): the two VCF-family catalog rows whose Broadcom upstream the backend can't dereference (an HTML developer portal) used to force operators to fetch the raw spec off a live appliance and upload it via the explicit-quadruple `--spec` shape (`catalog_entry_upstream_not_spec` 422). Both rows are now **profile-backed**: each ships a minimal, self-contained, `$ref`-local **OpenAPI 3.0** description of the read ops MEHO surfaces (vCenter: 9 inventory reads under `/api`; SDDC Manager: 9 inventory + lifecycle reads under `/v1`) plus a reviewed `ExecutionProfile` (vmware → `session_login` + `/api/about` fingerprint; sddc → `basic` + `/v1/releases/system` fingerprint), both carrying the SPDX `Apache-2.0` header and the vendor's verbatim path/param/field names (which the dispatcher must use to address the appliance). The rows flip `catalog_ingest` from `spec-only` to `supported` and drop their `upstream` (kept as provenance pointers in `notes`), so `meho connector ingest --catalog vmware/9.0` / `sddc/9.0` now succeeds end-to-end without an upload — the spec bytes load inline via `importlib.resources`, bypassing the fetch. Both artifacts are dry-run-parsed at boot by `validate_shipped_artifacts` with the live ingest parser; the named auth schemes match `docs/codebase/connector-auth-coverage.md`, and the typed `VmwareRestConnector` / `SddcManagerConnector` still own runtime dispatch. The full vendor specs stay the provenance pointers for a full-surface re-ingest (#1976).

### Added

- **Connector review drawer** on `/ui/connectors/registry` (G10.13-T3): the console half of the per-op connector-curation loop — setting each ingested op's `safety_level` / `requires_approval` / `is_enabled` / `custom_description` before a connector is dispatchable was CLI/MCP-only. A per-row (and post-ingest) **Review** button opens a `GET /ui/connectors/registry/{connector_id}/review` drawer that renders the shipped `ConnectorReviewPayload` as a **collapsed per-group `<details>` accordion** (name / `review_status` / `op_count`) — the **load-bearing big-payload guard**: a thousands-op connector (vmware-rest) never renders every op on first paint, each group's per-op rows **lazy-load** via `hx-get=".../review/groups/{group_key}"` fired on `<details>` open (`hx-trigger="toggle once"`). Each op row carries inline controls — a `safety_level` select, `requires_approval` / `is_enabled` toggles, and a `custom_description` field — that `PATCH /ui/connectors/registry/{connector_id}/operations/{op_id:path}` and re-render just that row (`hx-swap="outerHTML"`); the **`{op_id:path}` converter is load-bearing** so the natural key `f"{method}:{path}"` (e.g. `GET:/api/vcenter/cluster`) round-trips intact (it is the only `:path` param in the connectors surface — `connector_id` / `group_key` are slash-free plain params). The edit calls `edit_op_endpoint` **in-process** (the `forms_router` pattern, never the Bearer route) so the UI and Bearer-API writes share one validation + audit + warnings path; an `is_enabled=true` edit that returns an `unreplaced_auto_shim` advisory renders it **inline on the row** (the edit still landed — warnings never block). Every **loosening** edit (enable / drop approval / relax safety) is **confirm-gated** (`hx-confirm`) while a tightening edit applies directly. Read is `operator` (the per-op edit controls soft-hidden from non-admins via `resolve_role_probe`, read-only badges shown instead), the PATCH is `tenant_admin`-gated server-side via `resolve_operator_or_403` and CSRF double-submit gated; a `409 connector_scope_ambiguous` / `404` / `400` renders the shared inline error panel, not a 5xx. The literal `/registry/{connector_id}/review*` + `/operations/{op_id:path}` routes register ahead of the `/ui/connectors/{name}` catch-all (first-match-wins). The new `/ui/*` routes register into the OpenAPI snapshot + generated Go client, regenerated in lock-step (#1887).
- **`ProfiledRestConnector` + tri-state connector-dispatchability classifier** (G0.28-T1, the gating task of the REST-execution-profile initiative #1965): the foundation for making a spec-ingested REST connector **dispatchable from a reviewed declarative `ExecutionProfile`** instead of hand-written Python. A new `ProfiledRestConnector` is a **sibling** of the non-dispatchable `GenericRestConnector` auto-shim (an `HttpConnector` subclass, **not** a `GenericRestConnector` subclass), and a new tri-state `shim_kind` classifier (`none` hand-coded > `profiled` > `bare` auto-shim) replaces the binary `issubclass(GenericRestConnector)` "is this a dead shim" predicate at all six live sites (resolver tie-break, dispatcher `unreplaced_auto_shim` cause, the two ingest shadow-guards, the enable-time advisory, and the delete sweep). The resolver demotes by tier **before** the most-specific-version-match step, so a profiled connector beats a bare shim (it is dispatchable) but a bespoke hand-coded class still out-ranks a profiled one even when the profiled range is narrower — closing the #1750/#1798 product-shadowing footgun for the new tier before any profile lands. No operator-visible behaviour change yet (the profile schema and machinery land in #1969–#1974); the `register_connector_v2` product↔impl_id round-trip hard-fail still rejects a divergent profiled registration (#1967).
- **Profile-stamp review-gate interlock** (G0.28-T5, REST-execution-profile initiative #1965): stamping an `ExecutionProfile` onto an ingested connector makes it **dispatchable** but can never **auto-enable dispatch** — a security-load-bearing property. The new `ReviewService.record_profile_stamp(...)` seam registers the `ProfiledRestConnector` carrying the vetted profile but deliberately leaves every op `is_enabled=False` / `review_status='staged'`; dispatch against an unreviewed profiled op is blocked by the same `is_enabled = TRUE` filter in `lookup_descriptor` that hides a staged bare-shim op, until an operator clears the gate per-op (`edit_op --enable`) or connector-wide (`enable_connector`). The first stamp emits one `meho.connector.profile_stamp` audit row (re-stamps are idempotent — no duplicate); a non-profiled class raises `TypeError`. The enable-time advisory is now tri-state: a bare-shim resolve still yields the `unreplaced_auto_shim` dead-end warning, and a profiled resolve yields a new `profiled_but_unreviewed` `EditOpWarning` confirming the enable — not the stamp — cleared the review gate (regenerated OpenAPI snapshot + Go client; the review-drawer template echoes it verbatim) (#1971).
- **Declarative fingerprint/probe + named pagination from the `ExecutionProfile`** (G0.28-T6, REST-execution-profile initiative #1965): a profiled connector's `fingerprint` / `probe` and list-op pagination now come from reviewed declarative data instead of the auto-shim's `reachable=False` placeholder — **with no path/expression DSL**. The `ExecutionProfile` schema gains a `FingerprintSpec` (GET `path` + `authenticated` flag + a literal top-level `version_key` + a **named** `version_splitter` enum — `none` / `dash` (harbor's `v2.11.0-abc1234` → `(v2.11.0, abc1234)`) / `vrli_five_part` (vRLI's `9.0.0.0.21761695` → `(9.0.0, 21761695)`), grounded in the hand-coded `_parse_harbor_version` / `_parse_vrli_version`, not a free regex), a `probe` field that is either the `'delegate'` sentinel (probe via the fingerprint round-trip) or a `ProbeSpec` (health `path` + literal `ok_field` + `ok_value`), and a `PaginationSpec` selecting one closed strategy (`none` | `cursor_token`). `ProfiledRestConnector.fingerprint`/`probe` execute those recipes; `dispatch_ingested` follows the `cursor_token` cursor end-to-end for an ingested list op (the gcloud `nextPageToken` shape), unwrapping each page's literal top-level `items_key` and concatenating. **Response-field selection is always a single literal top-level key** — `version_key` / `ok_field` / `items_key` / the cursor's `resp_field` reject any `.` / `[` / `]` / `*` at the schema boundary (the #1177 "no JSONPath" line, mechanically enforced). Link-header / offset pagination is explicitly net-new and out of scope (#1972).
- **Authoring-mode `kind` / `dispatchable` on the connector REST API + CLI** (G0.28-T6, REST-execution-profile initiative #1965): `GET /api/v1/connectors` and `GET /api/v1/connectors/{id}/review` now distinguish **typed / ingested-shim / profiled / profiled-but-unreviewed** connectors via a new **additive** `kind` field plus a `dispatchable` boolean on `ConnectorListItem` + `ConnectorReviewPayload` — the existing dispatch-resolution `state` Literal (`ingested` / `registered`) is deliberately left unchanged (`state` answers "do descriptor rows exist", `kind` answers "what backs the connector and can it execute"). The pair is projected from the #1967 resolver `shim_kind` tier crossed with the #1971 review gate (`shared resolve_authoring_kind` helper), so a working profiled connector reads `kind=profiled` / `dispatchable=true` while a bare unreplaced shim — or a profiled connector whose review gate is still closed — reads non-dispatchable. The `meho connector list` / `review` output renders the new field (a trailing `*` marks a non-dispatchable connector; `--json` carries the raw fields) — the list route is untyped so the hand-maintained `listEntry` struct mirrors the keys; the typed review route regenerates the OpenAPI snapshot + Go client. Per-scheme auth detail stays off the wire until the `ExecutionProfile` schema freezes (#1969); the operator-console rendering is a separate task (#1980) (#1979).
- **Profile-declared session-expiry / auth-failure status set as one source** (G0.28-T7, REST-execution-profile initiative #1965): an `ExecutionProfile` now carries `expiry_statuses` (`frozenset[int]`, default `{401}`; vRLI declares `{401, 440}`) — the **single profile-declared source** for which non-2xx statuses a profiled connector treats as a session expiry / auth failure, consumed by **both** the session-retry harness (T4 #1970) and the dispatcher's auth-class classification arm, replacing the previously duplicated connector-side `_SESSION_EXPIRED_STATUSES` + dispatcher-side `_AUTH_FAILED_STATUSES` for profiled connectors. The field is a **narrowing of the recognised set, not a status→action map** — a `field_validator` requires `401` (the connector-agnostic session-expiry floor) and admits only additional 4xx vendor session-expiry codes (`>=440, <500`), rejecting `403`/`422` (their own dispatcher arms) and `404`/`429`/5xx (non-auth `connector_error`) so classification stays central. `is_auth_failed_status(status_code, expiry_statuses=...)` takes the profile's set when supplied (the dispatcher threads it via `_profile_expiry_statuses(connector_instance)`, read from `connector_instance.profile`), falling back to the unchanged `_AUTH_FAILED_STATUSES` global for typed (hand-coded, profile-less) connectors — typed-connector classification is unchanged (#1995).

### Changed

- **Connector review / enable-reads `prefer=tenant\|builtin` scope selector** (#2029): when a `connector_id` resolves to **both** a tenant-curated row and a built-in (`tenant_id IS NULL`) row, `GET /api/v1/connectors/{id}/review` and `POST /api/v1/connectors/{id}/enable-reads` (and the `meho.connector.review` / `meho.connector.enable_reads` MCP tools) still fail loud by default with the structured `connector_scope_ambiguous` 409 / `-32602` (#1801 preserved) — but they now accept an optional closed-set `prefer=tenant|builtin` selector that resolves directly to the named scope and skips the ambiguity probe, so an operator who curated a tenant row over a shipped built-in can finally target one without the error being a dead end. The ambiguous message itself now names the remedy (`retry with prefer=tenant or prefer=builtin`). `prefer=builtin` on the write path stays `tenant_admin`-gated at the route; omitting `prefer` is byte-identical to today. Regenerated the OpenAPI snapshot + Go client for the new query param.

### Fixed — operator console (topology diff)

- **Topology diff `datetime-local` pickers render blank** on `/ui/topology/diff` (#2014): the diff panel echoes the submitted `ts1` / `ts2` window back into its two `<input type="datetime-local">` pickers so a refine-submit starts from the current bounds, but it echoed the tz-aware (UTC) bounds via `datetime.isoformat()`, yielding an offset-bearing `…+00:00` string. Per the HTML standard a `datetime-local` value must be a normalized **local** date-and-time string (`YYYY-MM-DDTHH:MM`, no offset), so the user agent rejected the value and both pickers rendered **empty** on every re-render — forcing the operator to retype the window each time. The route now formats the two picker-echo values with `strftime("%Y-%m-%dT%H:%M")` (a format-only strip of the same submitted UTC wall-clock — no timezone conversion); the diff computation, the `ts1`/`ts2` query-param parsing, and the human-readable window summary are unchanged.

### Fixed

- **`ask_docs` synthesis leg 502 — force structured output + tolerant parse** (#1999): the v0.18.0 grounded-answer pipeline returned `502 {leg: synthesis_malformed, cause: parse}` on every query because the synthesis leg relied on prompt discipline alone for JSON and then ran a bare `json.loads`, which `claude-sonnet-4-6` broke by wrapping a longer answer in a ```` ```json ```` fence or a prose preamble. The synthesis call now **forces JSON via the Messages-API `output_config.format`** (the `_SynthesisOutput` schema; GA structured outputs on `claude-sonnet-4-6` — **no** `{` prefill, which 400s on that model family) through a new opt-in `StructuredJsonLlmClient.generate_structured_json` seam (the shared ingest-grouping `generate_json` path is byte-for-byte unchanged when no schema is requested). As defence in depth the parser strips a fence + prose preamble (shared `extract_json_object`) before `json.loads`; the **expand** leg gets the same fence tolerance (it shared the bug). The synthesis client now threads the model's `stop_reason`, so a token-ceiling cutoff raises the new `cause=truncated` (was mis-folded into `cause=parse`) and the parse-failure log carries `stop_reason` + a **bounded** raw head/tail (never the full body); the answer-leg token ceiling was raised 1024 → 2048.
- **Ingested-dispatch transport: verb-honoring, form-encoded bodies, header-param forwarding** (G0.28-T2 of the REST-execution-profile initiative #1965; supersedes #1664): three transport defects in the `source_kind='ingested'` dispatch path are fixed. (1) `dispatch_ingested` routed **every** non-idempotent verb through a hardcoded-`POST` `_post_json`, so an ingested `PUT`/`PATCH`/`DELETE` was silently sent as a `POST`; `_post_json` now honors a `verb=` parameter (validated to be non-idempotent) and the dispatcher forwards the descriptor's real method. (2) `_post_json` gained a form-encoded `data=` body path (`application/x-www-form-urlencoded`, mutually exclusive with `json=`) for the OAuth2 token-grant + session-login POST shapes the profiled-auth schemes (T4) will consume. (3) The header-located params bucket (`x-meho-param-loc: "header"`) was computed by `_split_ingested_params` then dropped; it is now surfaced on `IngestedRequest.headers` and forwarded to both transport seams as `extra_headers=` (merged onto `auth_headers`, per-call values winning). The idempotent `_request_json` retry/timeout invariant (#1178) is unchanged. The path-aware vcf-automation / vcf-operations transport overrides thread the new kwargs through their per-plane retry dance (#1968).

### Fixed

- **Operator-console "All" filter no-op** on `/ui/*` list views (#1963): the HTMX filter `<select>`s render an "All" `<option value="">`, so picking "All" (or clearing a filter — `hx-include` resubmits the sibling's empty value too) submitted an empty `status=` / `kind=` that failed the handler's `Literal[...] \| None` / `StrEnum \| None` query validation with HTTP 422. HTMX won't swap a 4xx, so the control silently no-op'd — the list never refreshed to the unfiltered view. A shared `BeforeValidator` (`meho_backplane.ui.query_filters.EMPTY_STR_TO_NONE`) now coerces the empty string to `None` **before** the literal/enum check on the runbook-runs, runbook-catalog, scheduler (`kind` + `status`), and agent-runs list filters, so "All"/cleared returns 200 with the unfiltered fragment. A genuinely out-of-vocabulary value (`?status=bogus`) still 422s — that rejection contract is unchanged.

### Added — operator console (Goal #336: G10.13 / G10.15 / G10.17 / G10.18 / G10.19 + retrieval)

- **Connector ingest modal** on `/ui/connectors/registry` (G10.13) — catalog or explicit-quadruple ingest from the console, with dry-run + async job-poll (#1951).
- **Vault / secrets console** (G10.18) — read-only KV browser (target/mount picker → list/read/versions, redaction-aware) (#1956); confirm-gated KV writes (`put` CAS / `delete` / `secret.move`) with approval handoff (#1957); and a read-only status view (seal/health/mounts + auth-methods glance) (#1958).
- **Keycloak realm-administration console** (G10.19) — read-only realm/client/client-scope browse (#1959); user management (list + create / reset-password / role-assign, tenant_admin, approval handoff) (#1960); and client-scope + protocol-mapper authoring (#1961).
- **Topology console — curated-edge writes + temporal views** (G10.17) — curated-edge annotate/unannotate writes (in-process BFF, tenant_admin) (#1953); bulk-import (dry-run/apply, tenant_admin) + refresh-from-target (operator) (#1954); and temporal read views — timeline / history / diff (operator) (#1955).
- **Audit forensic console** (G10.15) — `/ui/audit` filter-over-history query page with a forward-cursor pager + who-touched / by-work-ref / replay pivots (#1977); a deep-linkable row-detail drawer (by `audit_id`) + my-recent quick view (#1984); and a tenant_admin session-replay tree with a 413 over-cap flat fallback (#1986).
- **Account page** `/ui/account` — real whoami / role / tenant / expiry + an active-session list with confirm-gated revoke-this / revoke-others (#1991).
- **Broadcast Overrides tab** `/ui/broadcast` — tenant_admin detail-override rules table + glob create + delete-confirm (re-exposure) (#1992).
- **Retrieval console tabs** `/ui/retrieval` — Usage Analytics + Eval Quality tabs (#1950) and a Retire-checklist tab (5-criterion three-state verdict, platform_admin tenant filter) (#1952).
- **Profiled-connector surfacing** on `/ui/connectors/registry` — registry `kind` chip + filter, review-drawer staged badge, and ingest-modal local-spec indicator (#1980 / #2024).

### Added — config-driven connectors (ExecutionProfile schema)

- **`ExecutionProfile` schema + closed named auth catalog** (G0.28-T3 #1988): the reviewed declarative schema the profiled-connector machinery (T4–T7) consumes — a closed `AuthSchemeName` catalog validated at boot, with no free-form login/body DSL on the profile (the #1177 / #1969 closed-catalog line). Internal schema; no operator-visible behaviour on its own.

### Fixed — connectors + MCP + docs-search

- **Decouple TLS SNI / cert-verify host from the HTTP `Host` header** via a `tls_server_name` target field (#2002): a connector target fronted by a name-based proxy / SAN-mismatched cert can now verify TLS against the real server name while sending the operator's intended `Host`.
- **Honour RFC6570 reserved expansion in ingested-op path substitution** (#2020): ingested-op path params that contain reserved characters expand per RFC6570 instead of being over-encoded, so the dispatched URL addresses the real resource.
- **Map a scheduler Vault-write failure to JSON-RPC `-32602`, not opaque `-32603`** (MCP) (#2016): an agent-principal register whose scheduler-token Vault write fails now returns a structured, actionable error instead of a bare internal-error.
- **Model corpus `document_id` as `Optional`, coercing blank → `None`** (docs-search) (#2004 / #2019): a blank `document_id` no longer trips downstream model validation.
- **Migration 0049 — retire stale long-product (`vcf-logs`) ingest orphan rows** (#2001 / #2015): the historical product-namespace split left orphan `endpoint_descriptor` rows under the long product token; the migration retires them so dispatch resolves the canonical connector.

### Changed — CI / test-infra

- **Drop `--cov` from the unit lane** to stop an OOM kill on the heavy runner (#1982).
- **Restore SonarCloud unit coverage** via a push-only offline-combine job (#1987 / #1994).

### Docs

- **Reconcile the `meho-docs-addon` `/search` wire-contract** to the post-#1732 dialect (#2032).

### Dependencies

- Bumped: `actions/checkout` 6.0.3 → 7.0.0 (#2039), `trufflesecurity/trufflehog` 3.95.5 → 3.95.6 (#2038), `duckdb` 1.5.3 → 1.5.4 (#2044), `sqlalchemy[asyncio]` (#2046), `pytest` 9.1.0 → 9.1.1 (#2045), `ruff` 0.15.17 → 0.15.18 (#2041), `google-auth` 2.54.0 → 2.55.0 (#2040).

## [0.18.0] - 2026-06-19

### Added

- **Operations launcher console** at `/ui/operations` (G10.9-T1): the read-only entry surface for finding a runnable operation across the catalog from the web console — previously only the CLI (`meho operation groups` / `meho operation search`) could. A connector picker (populated from the connector listing, so every option is the dispatchable `<impl_id>-<version>` id — `vault-1.x`, not the bare `vault` slug that names no connector), an operation-group browse rail, and a debounced (`keyup changed delay:300ms`) hybrid BM25+cosine free-text search box with shareable (`hx-push-url`) results carrying each hit's `safety_level` + `requires_approval` badge. Each result opens a read-only **operation detail drawer** (`GET /ui/operations/descriptor/{id}`) showing the descriptor's operator-safe fields (`op_id` / `summary` / `description` / `method` / `path` / `safety_level` / `requires_approval` / `parameter_schema`); the per-op `llm_instructions` agent prompt renders **only** for a `tenant_admin` (soft-hidden + server-side gated for plain operators, since leaking the prompt is an injection vector). An unknown `connector_id` renders the typed unknown-connector hint and a registered-but-not-ingested one renders its `meho connector ingest …` `next_step` rather than a silent empty result. A thin BFF over the existing operation meta-tools (`list_operation_groups` / `search_operations` / `describe_descriptor`) called in-process — no new backend route or meta-tool. The `/ui/*` routes register into the OpenAPI snapshot and the generated Go client, regenerated in lock-step (#1879).
- **Operations Preview panel** inside the `/ui/operations` detail drawer (G10.9-T2): for an HTTP-ingested op, given a target + params an operator can render the **literal would-be HTTP request** (`method` / `resolved_path` / `query` / a pretty-printed `redacted_body` with a clear "redacted — secrets masked" note) **without dispatching it** — the read-only diagnosis sibling of Run (T3). `POST /ui/operations/preview` is a session BFF over the existing `preview_operation` meta-tool called in-process (the Bearer-gated `POST /api/v1/operations/preview` cannot be authenticated by a session cookie), CSRF double-submit gated like every `/ui/*` write, and operator-tier (preview is in the operator capability set — no `tenant_admin` step). The body is redacted through the **same** connector-boundary pipeline the response path uses, so it is not a new raw-secret surface, and **no audit row is written** (the `params_hash` privacy design is untouched). Operator-input faults (unknown op, invalid params, unresolvable connector) render inline as the in-envelope `status="error"`/`"unavailable"` + `extras.error_code` at HTTP 200; a missing target name surfaces as an inline 400 form error. The Preview button reads as safe (labelled distinctly from Run, "no dispatch") and the drawer GET re-sets the `meho_csrf` cookie so the form's double-submit pair lines up after the HTMX swap. The new route registers into the OpenAPI snapshot + generated Go client, regenerated in lock-step (#1880).
- **Doc-collection detail + lifecycle** on `/ui/corpus/collections` (G10.10-T2): each Collections-table row now links to a per-collection **detail page** (`GET /ui/corpus/collections/{collection_key}`) showing the collection identity (key / vendor / products), a **readiness card** (status pill / `doc_count` / `last_ingested_at` / the managed-RAG `index_built` "reachable ≠ answerable" flag), and — **only for a `tenant_admin`** — the server-side-only `backend{type, ref}` routing record (a plain operator never sees the `ref`). A tenant_admin can **re-probe** the collection (`POST .../{collection_key}/probe`) to refresh liveness without a page reload: the readiness card is swapped in place (`hx-target="#collection-readiness-card"`), an unreachable backend renders a `503` alert while leaving the row's `status` **unchanged** (success-only write-back), and the slow-probe button disables itself + shows a pending indicator (`hx-disabled-elt` / `hx-indicator`) so a serialized rebuild does not look hung. **Disable** (`POST .../disable`) is availability-destructive — a disabled collection fails `search_docs` with a terminal `403 collection_disabled` for **all** searchers — so it is fronted by a confirm modal spelling out that impact; **enable** (`POST .../enable`) is non-destructive (plain confirmed button). Both drive the in-process `set_collection_enabled` service (idempotent no-op on a re-call), and a forbidden lifecycle move renders a legible `409 invalid_collection_transition` alert, not a stack trace. The `/ui/corpus` unprovisioned empty state now links the in-console register flow for a `tenant_admin` instead of the old "ask an administrator" dead end. Every write is CSRF double-submit gated and server-side `tenant_admin`-gated; the new `/ui/*` routes register into the OpenAPI snapshot + generated Go client, regenerated in lock-step (#1883).
- **Operations Run modal** on `/ui/operations` (G10.9-T3): the console's first **execution** path — an operator can now confirm-and-dispatch a real connector op from the web, the surface previously reachable only via `meho <connector> <op>`. The detail drawer's **Run** button opens a `GET /ui/operations/run/{descriptor_id}` confirm modal carrying the target / params / optional `work_ref` inputs and an **unmissable** `safety_level` / `requires_approval` banner (a `caution`/`dangerous` or approval-gated op gets the loud warning treatment); confirming fires `POST /ui/operations/call`, a session BFF over the existing `call_operation` meta-tool called in-process (the Bearer-gated `POST /api/v1/operations/call` cannot be authenticated by a session cookie), CSRF double-submit gated and operator-tier (the policy gate — not RBAC — escalates a `requires_approval` op). The dispatched `OperationResult` renders inline: `status="ok"` shows the result, or, when the payload spilled out-of-band, the `ResultHandle` metadata (`handle_id` / row count / TTL / summary) rather than a huge blob; `status="error"`/`"denied"` shows the `extras.error_code` + the human message; **`status="awaiting_approval"`** surfaces the parked-request banner with `extras["approval_request_id"]` and a deep-link into `/ui/approvals/{id}` — so a governed write (since #1401 a human operator on a `requires_approval` op routes to the approval queue) is never shown as a silent / empty success. The confirm button carries its own CSRF `hx-headers` echo and disables during dispatch (`hx-disabled-elt`) so a high-impact op cannot be double-fired; a malformed `params` JSON or a missing target name renders inline as a 400 form error. The new routes register into the OpenAPI snapshot + generated Go client, regenerated in lock-step (#1881).
- **Corpus-aware query expansion before `ask_docs` retrieval** (Initiative #1912): `ask_docs` (the grounded-answer MCP tool) now runs a corpus-aware **expand** step before retrieval instead of passing the operator's question verbatim — the pipeline is **expand → retrieve-per-variant → RRF-merge → synthesize**. A terse / acronym-heavy question is rewritten into a small bounded set of query variants (≤4, the original always included) using the target collection's manifest (`vendor` / `products` / `description` / `when_to_use`, read straight off the resolved `doc_collections` row — no schema change), so the LLM expands acronyms and product synonyms in the corpus's own domain terms; retrieval then runs once per variant on the same backend and the per-variant chunks are merged with the existing reciprocal-rank fusion (`rrf_merge`, `RRF_K=60`) before synthesis. Expansion reuses the same #1386 fail-closed Anthropic ingest client as synthesis: an unconfigured model (`LlmClientUnavailable`) or an unusable expansion output (`DocsQueryExpansionError`, a distinct exception type) fails closed → `-32603`, never an un-expanded / ungrounded answer. `search_docs` (the raw-chunks agent path) is deliberately unchanged — expansion is the answer-pipeline's job only. No new REST route, no DSL or tunable ranking knob (#1916).
- **`vmware` doc-corpus manifest seed** (Initiative #1912): migration `0048` fills the global `vmware` `doc_collections` row's hand-authored `description` / `when_to_use` (and a canonical `vendor` / `products` when still unset), so the corpus-aware expand step (#1916) — which reads those manifest fields off the resolved row and omits empty ones from its prompt — grounds its query rewrites in VMware/Broadcom domain terms (vSphere, VCF, NSX, vSAN, Aria/vRealize) instead of expanding on a thin manifest. The migration is fill-only (never clobbers an operator-authored manifest), global-scope only, and an `UPDATE` not an `INSERT` — it enriches the operator-seeded shared row and is a clean no-op on a deploy where that row does not yet exist (it cannot author the deploy-specific NOT-NULL `backend` routing record). Manifest prose stays hand-authored — no auto-summarisation at ingest (#1920).
- **Runbook runs console** at `/ui/runbooks/runs` (G10.11-T1): the entry surface for the runbook **run** lifecycle on the console — starting a run and seeing the runs you own was `meho runbook start` / `meho runbook list-runs` (CLI) only. A new "Runs" tab on the existing `/ui/runbooks` surface lists runs role-scoped (an `OPERATOR` sees only their own runs, a `TENANT_ADMIN` sees every tenant run and can filter by assignee — the split is service-enforced, not just button-hiding) as a DaisyUI table of template@version / target / state badge / step position / work_ref / started_at, each row linking to the forthcoming run driver (T2). A **+ Start run** modal (`GET /ui/runbooks/runs/start`) picks a published template (free text or a datalist of the tenant's published slugs), a target, optional JSON `params`, and an optional `work_ref`; `POST /ui/runbooks/runs` is a session BFF calling `RunbookRunService.start_run` in-process (auto-assigning the operator), returning `HX-Redirect` into the driver on success and rendering inline `alert-error` (HTTP 200, not a 500) for a deprecated / not-found template or a missing `${run.params.X}` so the operator sees which key to supply. CSRF double-submit gated (the modal refreshes the `meho_csrf` cookie so the immediate submit's token matches). The `/ui/*` routes register into the OpenAPI snapshot + generated Go client, regenerated in lock-step (#1884).
- **Structured per-leg `ask_docs` answer errors** (Initiative #1912): an `ask_docs` failure no longer collapses to an opaque MCP `-32603 "internal error: <ClassName>"` that a consumer could not tell a config gap from a backend outage from a model-output bug (`claude-rdc-hetzner-dc#1407` gap 2). Each of the four answer-pipeline legs now surfaces a **distinct** structured error over the JSON-RPC `error.data` member naming *which* leg failed and *why*: `expand_failed` (the #1916 expand step — no model / unusable expansion), `corpus_unavailable` (retrieval backend down), `model_unavailable` (no / non-Anthropic synthesis key), and `synthesis_malformed` (the model ran but its output broke the grounding contract). `DocsSynthesisError` now carries a sub-cause splitting the two structurally-distinct synthesis failures the message previously buried — `parse` (output didn't parse into the required shape) vs. `citation_resolution` (a cited id didn't resolve to a retrieved chunk) — which point at different fixes. The classification lives in one framework-agnostic module (`docs_search/answer_errors.py`) producing a JSON-safe `{detail, leg, cause, message}` envelope (the code stays `-32603`; the new `McpInternalError` sentinel carries `data`), REST-ready so the forthcoming `POST /api/v1/ask_docs` (#1917) reuses it for 4xx/5xx — mirroring the connector-ingest dual-surface `error_envelopes.py` precedent. The answer stays **fail-closed** (never an ungrounded answer); a reusable `/ui/corpus` fail-open seam (`corpus_ask_fallback_context` + a `_results.html` branch) renders the retrieved chunks with the failed leg named, ready for the #1917 Ask mode to wire in (#1918).
- **Runbook run driver** at `/ui/runbooks/runs/{run_id}` (G10.11-T2): the console half that closes the runbook loop — a junior operator drives a started run step-by-step to completion or a reasoned abort, entirely in the web, with a senior able to reassign (previously `meho runbook next` / `abort` / `reassign` CLI only). The page renders **only the run's current step** (title, Markdown body, op_id/params, the verify gate) plus a "step n of total" position — never the full template, so it cannot re-open the skip-ahead leak the opacity floor (#1198) closed; the load-bearing guard is a new thin, opacity-safe `RunbookRunService.get_current_step` read that returns the SAME single-step `CurrentStepResponse` projection `start_run`/`next_step` return (never the step list). **Advance** is shown only to the run's assignee and is **service-enforced fail-closed** — a non-assignee (including a `TENANT_ADMIN`) gets `NotRunAssigneeError`, surfaced as an inline "reassigned away from you" message at HTTP 200, never a 500; a `confirm` step answered `no`/`escalate` fails the step and renders a dead-end banner whose only forward move is Abort. **Abort** requires a non-empty reason (guarded client-side AND server-side, persisted to the abort audit row so the guarantee holds even on a tampered form). **Reassign** is `tenant_admin`-only via the `require_ui_admin` hard dependency (an operator is 403'd before the service is touched). CSRF double-submit gated (every fragment re-mints the token + refreshes the `meho_csrf` cookie). The `/ui/*` routes register into the OpenAPI snapshot + generated Go client, regenerated in lock-step (#1893).
- **Connector registry console** at `/ui/connectors/registry` (G10.13-T1): the first console surface of the connector-curation loop, a role-scoped registry list **distinct** from the existing `/ui/connectors` **targets** list (surfaced as its own "Connector Registry" sidebar entry so the two never conflate). It lists the ingested/typed/composite connectors via the in-process `list_ingested_connectors` (operator-scoped visibility: built-ins + the caller's own tenant, never cross-tenant), each row showing `connector_id` / product / version / impl_id, a tenant-vs-built-in chip, the `ingested`/`registered` state pill, and the group/op counts; a `?status=staged|enabled|disabled|all` enum filter (the `all` **sentinel**, never an empty-string option that would 422 the `ConnectorStatusFilter` enum and silently no-op the HTMX swap) and a `?product=` exact-match dropdown computed in the handler from the returned rows narrow the view. Per-row **enable** / **enable-reads** / **disable** / **delete** verbs (`POST /ui/connectors/registry/{connector_id}/enable|disable|enable-reads` + `DELETE /ui/connectors/registry/{connector_id}`) call the shipped REST handlers **in-process** so the UI and Bearer-API writes share one validation + state-machine + audit path; read is `operator` (the write affordances soft-hidden from non-admins via `resolve_role_probe`), writes are `tenant_admin`-gated server-side via `resolve_operator_or_403`. Every loosening action is confirm-gated — enable / enable-reads / disable front a modal naming the projected blast radius, and delete is **type-to-confirm** (retype the `connector_id`) surfacing the `enabled_operations_deleted` advisory. A `409 connector_scope_ambiguous` (with the candidate tenant-vs-built-in scopes) or an `InvalidStateTransitionError` `409` from the in-process handler renders an **inline actionable panel** against the row, not a 5xx; a successful verb re-renders the affected row via `hx-swap-oob` (a delete returns an empty OOB stub that drops the row). Every write is CSRF double-submit gated; the new `/ui/*` routes register into the OpenAPI snapshot + generated Go client, regenerated in lock-step (#1885).
- **Connector ingest modal + async job-poll** on `/ui/connectors/registry` (G10.13-T2): the console's first connector-ingest on-ramp — parsing a vendor OpenAPI spec into the registry was CLI/MCP-only. A tenant_admin-only **Ingest** button (soft-hidden from operators) opens a `GET /ui/connectors/registry/ingest` modal with two mutually-exclusive modes — **catalog** (a dropdown read in-process from `catalog_endpoint`) and **explicit quadruple** (`product`/`version`/`impl_id` + one-or-more `https://` `specs[].uri` rows; inline byte upload stays CLI-only) — plus a **Dry-run** toggle. `POST /ui/connectors/registry/ingest` builds exactly one `IngestRequest` shape (a handler pre-check renders a friendly inline error rather than the raw `catalog_entry_conflict`/underspecified 422) and calls `ingest_endpoint` **in-process** (the `forms_router` pattern, never the Bearer route): a dry-run renders the sync parse counts and **writes nothing**, while a real ingest kicks the async job and seeds a **self-polling** job fragment. `GET /ui/connectors/registry/ingest/jobs/{job_id}` renders `_ingest_job_status.html`, self-polling via `hx-trigger="every Ns"` while `running` and **dropping** the poll directive on a terminal status (the htmx "stop returning the polling element" idiom); a `degraded` job shows the counts **and** the `ingested_not_dispatchable` reason (never a bare success), and — the load-bearing process-local-jobs guard — a **404 after a pod restart** renders a "job lost, re-check the registry list" panel and **stops polling** rather than spinning forever. The catalog-resolution 422s (with `available_entries[]`), the explicit-shape 422s, the 400 spec-parse family, and `503 LlmClientUnavailable` all render as actionable inline panels via the T1 shape-generic error renderer. All three routes are `tenant_admin`-gated via `resolve_operator_or_403`; the submit is CSRF double-submit gated; the literal `/registry/ingest*` routes register ahead of both the `/ui/connectors/{name}` catch-all and the `/registry/{connector_id}` param routes (first-match-wins). The new `/ui/*` routes register into the OpenAPI snapshot + generated Go client, regenerated in lock-step (#1886).
- **`ask_docs` over REST + an `/ui/corpus` Ask mode** (Initiative #1912): `ask_docs` (the grounded-answer pipeline) was MCP-only — `openapi.json` exposed `/api/v1/search_docs` (chunks) but a POST to `/api/v1/ask_docs` 404ed, so the `/ui/corpus` BFF could only render raw chunks. A new **`POST /api/v1/ask_docs`** is the synthesis sibling of `search_docs`: same `operator` role + per-collection `meho-docs:<collection>` entitlement + readiness gate (`403 not_entitled` / terminal `403 collection_disabled` / `409` not-ready / `422` missing-or-unknown collection — including a cross-tenant / absent collection, which is invisible to the tenant-scoped catalogue — all mirror `search_docs`), single-collection only (no `collections` fan-out field), running the #1916 expand→retrieve-per-variant→RRF→synthesize pipeline in-process and returning `{answer, citations[]}` with #1919-resolved citation links (the **same** citation shape the MCP tool returns). The #1918 per-leg structured error model maps onto HTTP status — `502 synthesis_malformed` (the model answered but broke the grounding contract) vs. `503 expand_failed` / `corpus_unavailable` / `model_unavailable` (a server-side config / availability fault) — carrying the identical `{detail, leg, cause, message}` envelope the MCP face returns on `error.data`; the answer stays fail-closed (an empty retrieval is a normal `200` "no grounded answer"). `/ui/corpus` gains a **Retrieve / Ask** mode toggle: Ask renders the grounded answer + clickable citations, and on a synthesis-leg failure **fails open to the retrieved chunks** under a banner naming the failed leg (the #1918 `corpus_ask_fallback_context` seam) rather than discarding usable evidence — never an ungrounded answer. The REST route and the UI BFF share one in-process pipeline composition (the Bearer-gated route cannot be authed by a session cookie), and the UI write is CSRF double-submit gated like the search fragment. The new route registers into the OpenAPI snapshot + generated Go client, regenerated in lock-step (#1917).
- **Conventions console** at `/ui/conventions` (G10.12-T1): the read surface for the operator-authored rules packed into every agent session preamble — previously inspectable only via `meho conventions list` / `show` (CLI). The headline value is an **always-on preamble token-budget banner**: it renders the estimated/`600`-token math on every load and, when the operational set overflows the budget, switches to an error-styled banner listing every `dropped_slug` **in red** with explicit "dropped from agent preamble — agents never see this rule" copy, surfacing the otherwise-silent overflow drop (an `operational` rule the packer dropped is invisible to agents with no other signal). The banner reflects the full operational set regardless of the active **kind tab** (operational / workflow / reference), which HTMX-filter the summary table (slug / title / kind / priority, each row linking to detail) and push a bookmarkable URL; a `GET /ui/conventions/{slug}` detail view renders the full body through the sanitised `render_markdown` (`html=False`, so a body carrying `<script>` is escaped, not executed). Operator-read; calls the shared in-process `ConventionsService` (`list_conventions` / `get_convention`), so the budget arithmetic + `priority DESC, created_at ASC` ordering match the REST surface and the MCP `initialize` preamble packer exactly — no re-derived budget math in the UI layer. The literal `/ui/conventions` list route registers ahead of `/ui/conventions/{slug}` (first-match-wins). The `/ui/*` routes register into the OpenAPI snapshot + generated Go client, regenerated in lock-step (#1895).
- **Doc Collections lifecycle tab** on `/ui/corpus` (G10.10-T1): a Collections tab listing the tenant's registered doc-corpora (key / vendor / products / readiness pill / `doc_count`) with a tenant_admin **+ Register** modal (`POST /ui/corpus/collections`) to onboard a new collection (key / vendor / products / `backend{type, ref}`) from the console — previously `meho docs collections create` (CLI) only. Operator-read, tenant_admin-write (soft-hidden + server-side gated), CSRF double-submit gated; rows link to the per-collection detail page (T2). The `/ui/*` routes register into the OpenAPI snapshot + generated Go client in lock-step (#1882 / #1913).
- **Conventions authoring** on `/ui/conventions` (G10.12-T2): the write half of the conventions console — a tenant_admin can create / edit / delete a convention from the web (previously `meho conventions create` / `edit` / `delete` CLI only) via author/edit modals carrying a live **token-cost preview** (so an author sees the preamble-budget impact before saving), a **delete confirm**, and a **history diff** of prior revisions. Operator-read, tenant_admin-write, CSRF double-submit gated; calls the shared in-process `ConventionsService` so the budget math matches the preamble packer. The `/ui/*` routes register into the OpenAPI snapshot + generated Go client in lock-step (#1940).

### Changed

- The release-body honesty gate (`scripts/release/check_release_body_paths.py`) now validates the **HTTP method** of a verb-prefixed API citation against the OpenAPI path's registered methods, not just path existence — closing the class that let `[0.17.0]` ship a `POST /api/v1/operations/search` citation for a `GET`-only route (the `[0.17.0]` notes were corrected in place to `GET`) (G0.27 dogfood, #1914 / #1929).
- **SonarCloud unit-test coverage decoration restored without re-OOMing the CI gate.** #1982 had to drop `--cov` from the required `Python (ruff + mypy + pytest)` lane because running the unit suite under coverage peaked the pytest tree at ~13.67 GiB and OOM-killed the memory-limited `meho-runners-ci-heavy` pod, taking SonarCloud's Clean-as-You-Code coverage with it. Coverage now rides a dedicated, **push-to-main-only**, **non-required**, whole-job `continue-on-error` `python-coverage` job that runs the unit sweep serially (`-n 1`) with **offline `coverage combine` + `xml`** (per-worker parallel data via `COVERAGE_PROCESS_START` + `.coveragerc.ci`; pytest invoked `-p no:cov` so pytest-cov's memory-spiking in-process session-end combine never runs). Measured peak (cgroup `memory.peak`, full 8306-test suite): **12.48 GiB at `-n 1`** — under the pod limit that the old 13.67 GiB run tripped — vs 13.17 GiB at `-n 2` and 14.03 GiB at `-n 3` (the coverage memory tax is intrinsic and barely shrinks with xdist worker count, so the combine was never the peak). The required PR lane stays untouched (no-cov, `-n 3`, green); PRs get coverage decoration "one merge late" (#1987).

### Fixed

- **Connector-curation MCP tools no longer fail with a bare `-32603 internal error` on an ambiguous connector scope** (G0.27 dogfood, #1910). When a `connector_id` resolves to **both** a tenant-curated row and a built-in (global) row — the state a product-token reconciliation (#1814/#1817) plus a tenant re-ingest leaves behind — the two scope-resolving curation tools (`meho.connector.review`, `meho.connector.enable_reads`) raised `AmbiguousConnectorScopeError`, which fell through the MCP dispatcher's generic catch-all to a bare JSON-RPC `-32603 "internal error: AmbiguousConnectorScopeError"` with no remediation, blocking curation of the affected connector. The MCP path now surfaces the same structured `data` envelope the REST 409 already carried (`detail="connector_scope_ambiguous"` + the candidate `{product, version, impl_id, tenant_id}` scopes + a message) as a JSON-RPC `-32602`, so an operator/agent reads `error.data.candidates` and re-issues with the disambiguating `tenant_id` (`null` for the built-in scope, their own tenant UUID for the tenant row). Closes the last MCP↔REST asymmetry on the connector error surface (the ingest `SpecError` siblings were aligned earlier in #1534). Note: the stale long-product duplicate listing row that motivated the report is a deliberately-operator-owned cleanup (delete via `meho.connector.delete`); it does not itself cause the error and is left untouched.
- **`/ui/corpus` Ask mode now renders the retrieved chunks on a post-retrieval (synthesis/model) leg failure** instead of an empty fail-open banner (#1939). When the `ask_docs` answer pipeline broke **after** retrieval succeeded — the synthesis model emitting malformed output (`synthesis_malformed`) or the synthesis model being unconfigured (`model_unavailable`) — the Ask fail-open named the failed leg but showed **zero chunks**, dropping the good grounding the pipeline had already retrieved and defeating the "fail open to retrieval-only" guarantee exactly when it was most useful. The BFF now composes the pipeline via a new UI-only return channel (`run_ask_pipeline_capturing_retrieval` → `AskPipelineOutcome` in `api/v1/ask_docs.py`) that hands back the retrieved chunks alongside the classified leg error, so a post-retrieval failure renders those chunks under the named-leg banner — true retrieve-only fallback — while the pre-retrieval legs (`expand_failed` / `corpus_unavailable`, which produced no chunks) correctly stay banner-only. The answer path stays **fail-closed** (never an ungrounded synthesized answer); the raising `run_ask_pipeline` the REST `POST /api/v1/ask_docs` route uses is unchanged, and the framework-agnostic `AskDocsAnswerError` wire envelope (MCP `error.data` + REST 5xx) is untouched — the chunks ride an in-process Python channel only, never the wire. Origin: CodeRabbit Major M1 on #1937 (capstone of Initiative #1912).
- **The `vcf-logs` (vRLI) connector now recovers an expired session** instead of failing every call until a backplane restart (G0.27 dogfood, #1909). The session-retry re-authenticated only on HTTP `401`, but vRLI signals a recoverable expired session with **`440`** ("session ID expired — obtain a new session") — the one status the connector ignored — so a scheduled / long-running vRLI consumer logged in on the first call and then `connector_auth_failed`'d on every subsequent call once the idle session expired. The re-login trigger now treats `440` like `401` (per the vRLI spec), and the `connector_auth_failed` message no longer claims "retried once on a 401" for the unretried-440 case (#1921).
- **`vmware.composite.datastore.usage` no longer hard-fails when its optional VM-placement enrichment errors** (G0.27 dogfood, #1908). The composite's `GET /vcenter/vm?filter.datastores=…` enrichment leg 400s on a vCenter whose filtered-VM-list query param differs from the 9.0 spec (e.g. 8.0), and the whole composite threw a bare `RuntimeError` — discarding the capacity/free reading it had already retrieved. Enrichment is now **best-effort**: the core datastore rows return with the `vm_*` fields nulled when enrichment fails, and the sub-op's structured error (status + URL) is bubbled to the composite level instead of a bare exception, so the "what's about to fill up?" report survives a vCenter-version mismatch (#1922).

### Documentation

- Document that after a backplane upgrade an MCP client must **re-initialize** (restart the client/session) to see newly-shipped tools — a transport reconnect alone may keep serving the tool list cached at `initialize`; MEHO's catalog is immutable-per-process and served fresh per `tools/list`, so this is a client-/process-lifecycle behaviour, not a server cache (G0.27 dogfood, #1915 / #1928).

## [0.17.0] - 2026-06-19

### Added

- **Keycloak-aware approval write previews**: parking a Keycloak write op now surfaces a resource-centric, redaction-correct `proposed_effect` to the approver instead of the bare `{op_id, connector_id, target_id}` identifier default. Bespoke builders in `connectors/keycloak/ops_write_preview.py` hoist the human-meaningful identity of the resource being created/modified — `keycloak.realm.create` → the realm name, `keycloak.user.create` → the username, `keycloak.role_mapping.assign` → the granted realm role names — so the reviewer reads "creating user `svc-meho` in realm `meho`" directly. All secret material is scrubbed to `***REDACTED***` via the connector's own single-sourced `redact_secret_fields` before the preview lands in the durable approval row — covering the Keycloak representation secret fields (`secret`, `credentials`, `value`, `secretData`, `credentialData`) *and* the generic credential key spellings (notably `password`, matched case-insensitively), so a `RealmRepresentation.smtpServer.password` (the SMTP relay secret, carried in an `additionalProperties: true` body) or any other `password`-keyed field never reaches the durable row. This also closes the matching latent gap in the Keycloak read ops, which share the same scrubber. `keycloak.user.create` classifies as `credential_write` (which suppresses the generic params-echo default); the preview hook now lets a *bespoke* builder — trusted to own its field discipline — run even for a credential-class op (mirroring the permission-preflight hook), while the generic echo stays suppressed. Builders are fail-soft per the existing contract: a raise becomes the `preview_unavailable` marker, never blocking the park (#1857).
- **Operator agent run-cancel**: `POST /api/v1/agents/runs/{handle}/cancel` (REST) and `meho agent run-cancel <handle>` (CLI) let an operator stop a non-terminal agent run — previously only the internal reaper/scheduler could write `cancelled`. Both wrap the existing `cancel_run` service path, so the durable `cancelled` transition and its `agent_run.completed` lifecycle outbox event (emitted by the shared `transition` for every terminal state) are produced by one code path with no second status-write. The route is operator-role, tenant-scoped (an unknown / cross-tenant handle is `404`, no existence leak) and race-safe: a run that completes between the request and the write returns `409 agent_run_not_cancellable`, not a 500. An `awaiting_approval` run cancels cleanly; its pending approval is left to expire / be rejected on resume. The OpenAPI snapshot and the generated Go client are regenerated in lock-step (#1828).
- **Agent principals console** at `/ui/agents/principals` (G10.8-T4): the agent-identity inventory and the **Keycloak kill switch**, a sub-surface of the `/ui/agents` console. Operators view the tenant's registered principals (name / Keycloak client id / active-or-revoked pill / owner / registered-by / created-at, with an `include_revoked` toggle for audit); tenant_admins register new principals (the in-process `AgentPrincipalService.register` creates a Keycloak client + writes its generated credential to Vault) and revoke them. **Revoke is terminal and destructive** — it disables the Keycloak client, blocking all new token grants for the identity — so it is fronted by a strong native-`<dialog>` confirm requiring **type-to-confirm of the principal name** (gated client-side by Alpine and re-checked server-side, so a crafted POST cannot skip it). Register/revoke are tenant_admin-only (soft-hidden + hard `403`); the list is operator. Keycloak/Vault failures render the **actionable** backend detail (`503` `KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL`, `502` `keycloak_admin_error` / `scheduler_vault_write_error`) inline, not a generic error; all writes are CSRF double-submit gated; all queries are tenant-scoped (cross-tenant / absent → `404`). The `/ui/*` routes register into the OpenAPI snapshot and the generated Go client, regenerated in lock-step (#1831).
- **Operator agent control plane (G10.8)** — the server-rendered `/ui/agents` console for the agent runtime: the agents surface + agent-definitions CRUD (#1850), the `/ui/scheduler` trigger list/detail + create/cancel (#1849), the full-page `/ui/approvals` console with status-filterable decision history (#1848), the agent runs list + detail (`/ui/agents/runs`) with `work_ref`/status filters (#1865), a live agent run console streaming `invoker.stream_events` over SSE (#1867) with a wired Stop-run button over the run-cancel endpoint (#1878), and the `/ui/agents/grants` permission-grant surface — list / create / elevate / revoke (#1871). Every surface is tenant-scoped, role-gated (operator read / tenant_admin write), CSRF double-submit gated, and registers into the OpenAPI snapshot + generated Go client in lock-step. Together with the run-cancel REST/CLI verb (#1828) and the principals console + Keycloak kill-switch (#1831) this completes the G10.8 autonomous-execution control plane.
- **Approval `proposed_effect` now carries `safety_level`**: the catalog `EndpointDescriptor.safety_level` (safe / caution / dangerous) is promoted onto the approval row's `proposed_effect` envelope alongside `op_class`, so a reviewer can tell a parked `dangerous` op (e.g. `keycloak.realm.create`) from a `caution` op (e.g. `keycloak.user.create`) at a glance instead of reading structurally identical identifier payloads (#1855).
- **Generic redaction-safe params-echo for approval previews**: an approval-gated op with no bespoke preview builder now echoes its requested params into `proposed_effect` (redaction-safe, honouring the existing `_SENSITIVE_CLASSES` suppression) instead of the bare `{op_id, connector_id, target_id}` identifier default — so every connector gets param-level legibility for free, with bespoke builders (k8s / vmware / argocd / keycloak) layered on where richer previews help (#1874).
- **kb upsert is wire-correct + attributable**: a same-slug kb re-index now returns `HTTP 200` for an in-place overwrite (only a genuinely new slug returns `201`), and every kb read surface (`POST`/`GET /api/v1/kb`, `GET /api/v1/kb/{slug}`, `POST /api/v1/retrieve {source:"kb"}`) now carries `created_by_sub` / `last_updated_by_sub` write-attribution, so an operator sees who authored / last changed an entry without an audit-log join. The cross-principal upsert/delete semantics (tenant_admins mutually trusted — wiki-like) are documented (#1869).

### Changed

- **BREAKING (operator-facing product tokens + data migration):** the five remaining `_PRODUCT_SPLITS` connectors now register under their short, dispatch-canonical `product` token, matching what `meho connector list` already emits and what `parse_connector_id` derives from each connector-id: `sddc-manager`→`sddc`, `vcf-automation`→`vcfa`, `vcf-fleet`→`fleet`, `vcf-operations`→`vrops`, `hetzner-robot`→`hetzner` (`impl_id`/`version` unchanged). With vRLI (#1798) this completes the family realignment — every shipped connector round-trips `parse_connector_id` and `register_connector_v2` no longer WARNs at boot. `POST /api/v1/targets` now accepts the short tokens directly: the `TargetCreate.product` enum (and the regenerated CLI client) advertises `sddc`/`vcfa`/`fleet`/`vrops`/`hetzner`, the long spellings are rejected, and the now-redundant `PRODUCT_ALIASES["sddc"→"sddc-manager"]` entry was removed (a `product:"sddc"` create previously 422'd against the alias). Migration `0047` reconciles existing live `targets.product` rows long→short for the five mappings (idempotent, reversible, per-product-scoped, soft-deleted tombstones preserved) — mirroring vRLI's `0046` — so existing targets keep dispatching across the realignment instead of failing `NoMatchingConnector`. Operators with targets stored under a long token need no action (the migration moves them); any external tooling that hard-codes a long `product` string must switch to the short token. The Hetzner fingerprint's vendor-reported `product="robot-webservice"` is unchanged (it is not the registry token). (#1814)
- Promote the connector `product`↔`impl_id` round-trip check in `register_connector_v2` from an advisory WARN to a **hard fail** — now that the family is realigned (#1814) nothing diverges, so a future divergent registration crashes `_eager_import_connectors` at boot (a deploy-time typo) instead of silently shadowing the connector behind an auto-shim (#1816).
- **BREAKING (operator-facing ingest contract):** **all ingest entry points (REST / MCP / CLI)** now reject a supplied `product` that does not round-trip its connector_id, before any spec is fetched or row is written. The round-trip guard lives at the shared service-layer chokepoint `IngestionPipelineService.ingest` (not the REST handler alone), so `POST /api/v1/connectors/ingest` returns a `422 product_impl_id_mismatch`, the `meho.connector.ingest` MCP tool returns a JSON-RPC `-32602` carrying the same structured envelope, and the CLI verb fails closed — none silently persists a non-dispatchable shadow. An ingest of `--product <p> --impl-id <i>` where `parse_connector_id("<i>-<version>")` derives a *different* product (e.g. `--product drift-test --impl-id drift-impl` → derives `drift`; any long VCF-family token such as `--product vcf-automation --impl-id vcfa-rest` → derives `vcfa`; or `--product vcf-logs --impl-id vrli-rest`, whose impl_id is served by the hand-coded `VcfLogsConnector` registered under `vrli` → derives `vrli`) previously returned `200`/persisted under the divergent token over the MCP path (the REST `422` covered only the route); it now fails loud on every path and names the derived product to use. The guard is skipped when `version`/`impl_id` is empty or the connector_id parse is lossy (non-digit-leading version). Operators ingesting under the short, dispatch-canonical token (what `meho connector list` emits and the realigned family registers under, post-#1814) are unaffected; any tooling that hard-coded a long/divergent `--product` must switch to the derived token or rename `impl_id`. With divergent ingests rejected upstream of the write path, the now-dead long↔short row-reconciliation bridges were retired: `dispatch_product` / `_reconciled_row_product` and the `PRODUCT_ALIASES` / `canonical_product_token` write-time alias map are removed; ingested rows and `POST /api/v1/targets`/`PATCH /api/v1/targets/{name}` now persist the supplied product verbatim (the `register_connector_v2` hard-fail of #1816 remains the backstop) (#1817).
- **BREAKING (operator-facing list/search filter param):** the free-text filter query param is now canonically named **`q`** across the three list/search surfaces (`GET /api/v1/kb`, `GET /api/v1/memory`, `GET /api/v1/operations/search?connector_id=…&q=…`), replacing the three divergent per-surface names (`filter` / `slug_pattern` / `query`) that silently ignored a reasonable `?q=` guess on the wrong surface. The legacy names keep working as `deprecated:true` aliases (back-compat), but supplying both the canonical `q` and a conflicting legacy value now returns `422 ambiguous_free_text_filter` instead of silently picking one, and operations-search with neither returns `422 missing_query`. As a result the generated CLI `query` param flips required→optional, and both Go call sites (`operation search`, `dispatch.Search`) now send `q`; the OpenAPI snapshot + generated client are regenerated in lock-step. Operators using the documented per-surface name on its own surface are unaffected; any tooling that hard-coded one surface's filter name on another should switch to `q` (#1854 / #1868).
- **Dependency bumps**: `cryptography` 48→49 (#1768), `starlette` 1.0.1→1.3.1 + `aiohttp` 3.14.0→3.14.1 (#1823), plus dev/CI tooling — `ruff` (#1770), `pytest` 9.0→9.1 (#1771), `google-auth` (#1772), `pydantic-ai-slim` (#1769), `sonarqube-scan-action` (#1767). `fastapi` is held at `0.136.3` — the Dependabot bump to 0.137 (#1773) was reverted (see Fixed / #1820).

### Security

- Closed a within-tenant **cross-principal memory leak** on the retrieval data path: `POST /api/v1/retrieve {source:"memory"}` and the `meho://retrieve/{query}` MCP resource returned another principal's per-principal memories (`user` / `user-tenant` / `user-target` scopes, full `body`) to any operator in the same tenant. The two surfaces called the shared `retrieve()` substrate without the per-principal `user_sub` push-down the isolated read paths (`recall`, `list`, MCP `search_memory`) already apply — the HTTP route forwarded client `metadata_filters` verbatim with no per-principal scoping, and the MCP resource passed no filters at all. The fix enforces a **mandatory, non-overridable** per-principal predicate at the shared `retrieve()` boundary (`principal_sub`): for `source="memory"` user-scoped kinds a row is returned only when its stored `user_sub` equals the caller's `sub`, applied regardless of caller and AND-enforced so a client-supplied `metadata_filters={"user_sub":"<other>"}` cannot widen it. Tenant-broadcast scopes (`tenant` / `target`) stay visible cross-principal (no over-correction), and the other `retrieve()` sources (`knowledge` / `operations` / `docs`) are tenant-scoped and carry no per-principal `user_sub` data the same gap would leak. Bidirectional + non-override + no-over-correction probes added against a real pgvector cluster, covering both the HTTP route and the MCP resource (#1797).
- **Closed a stored-XSS vector in the runbook editor**: the editor template rendered untrusted values into a double-quoted Alpine `x-data` attribute, where a crafted value could break out of the attribute. The data-bearing island is now single-quoted, matching the output-encoding hardening (PR #1044) already applied to the other islands, closing the attribute-breakout vector (#100).
- **Gated the cross-session audit-replay route at `tenant_admin`**: replaying across another session's audit trail is now restricted to `tenant_admin`; it was previously reachable by the lower operator role (#1843).

### Fixed

- Unify connector-id resolution across `GET /api/v1/connectors/{connector_id}/review` and `POST /api/v1/connectors/{connector_id}/enable-reads` so a label resolves to the **same** row on both paths: `enable-reads` now honours the same tenant→built-in global fallback `review` had (a global-only connector enables its reads instead of returning 404), and a label that maps to **both** a tenant-curated row and a built-in row returns a structured `connector_scope_ambiguous` 409 listing the candidate rows on both paths — instead of `review` silently picking one and `enable-reads` 404'ing (#1801).
- Docs-corpus entitlement denials are now **diagnosable** instead of opaque: `/ui/corpus` names the missing `meho-docs:<collection>` capability + the resolved identity/tenant when a corpus exists but the session identity isn't entitled (distinct from the genuinely-unprovisioned empty state), and `POST /api/v1/search_docs` returns a structured `403` (`{"error":"not_entitled","collection","required_capability","operator_sub","tenant_id"}`) — so an operator can grant exactly the right claim. The MCP `search_docs` / `ask_docs` `-32602` carries the same on `error.data`. The three surfaces' entitlement contract is verified consistent (same `(tenant_id, capabilities)` derivation; the only divergence is the per-audience token MCP vs REST/UI validate), and the Keycloak per-audience `meho-docs:*` claim requirement is documented in `deploy/` (#1802).
- Operator-console modals now dismiss via the close button, the Escape key, and a backdrop click. The HTMX-injected `<dialog>` fragments relied on DaisyUI's static `modal-open` class, which the native `dialog.close()` cannot clear, so every modal (approvals, memory create/promote, connector create/edit/delete) stayed stuck open until a page reload. A shared app-shell controller now opens injected dialogs via `showModal()` on swap and strips any lingering `modal-open` on the native `close` event (#1803).
- Connector **auth/session failures** now surface as a structured `connector_auth_failed` dispatch result instead of the opaque `connector_error`. An upstream `401` (and vRLI's `440`) reaching the dispatcher names the host, the status, the likely cause (a session/credential expiry or a misconfigured `auth_model` — the session connectors already retry once on a 401 internally, so re-login *also* failed by then), and the verify-the-Vault-credential/`auth_model` remediation; the structured cause rides in `extras` (`error_code`, `http_status`, `host`, `upstream_message`). Mirrors the `connector_tls_verify_failed` (#1782) and `connector_http_403` (#1649) pattern. This closes the diagnosability gap that made the vRLI dispatch the operator saw as `connector_error (440)` (#1798) look like a stub-auth problem. Non-auth statuses (`404`, `5xx`, `429`) still flatten to `connector_error` unchanged (#1804).
- Fix a vRLI dispatch dead end where a `product="vrli"` target — the natural token `meho connector list` emits — resolved an auto-registered `GenericRestConnector` shim (`connector_unsupported` / `auth_headers` `NotImplementedError`) instead of the shipped `VcfLogsConnector`, because the connector registered under a divergent `product="vcf-logs"` namespace while the dispatcher derives `"vrli"` from the `vrli-rest` connector-id. `VcfLogsConnector` is now aligned to the dispatch-canonical `product="vrli"` (round-trips `parse_connector_id`), spec-ingest defers to a hand-coded connector instead of scaffolding a shadowing shim under a divergent product token, and `register_connector_v2` logs a structured WARN (not a boot failure) when a registration's `product` ≠ its connector-id-derived product. A migration reconciles existing `product="vcf-logs"` targets to `"vrli"` (no new `PRODUCT_ALIASES` entry). The five remaining sanctioned `_PRODUCT_SPLITS` connectors WARN, pointing at the family-realignment Initiative #1810 (#1798).
- `meho targets import --dry-run` is now existence-aware: it routes through the live planner (one read-only `GET /api/v1/targets`, zero writes), so an existing target previews as `UPDATE` instead of the misleading `CREATE` the offline planner always showed; a brand-new target still previews as `CREATE`. The dry-run godoc is aligned to the read-only behaviour (#1785 / #1861 / #1870).
- Pin `fastapi <0.137`: 0.137.0 drops the entire `/api` router surface from `app.routes` (and leaks a handler-500 at `TestClient` teardown), so the Dependabot bump was reverted and the dependency held at `0.136.3` pending a compatibility pass (#1820).
- Deflaked `test_corpus_client.py::test_forwarded_jwt_never_logged`, which
  asserted on structlog events from a module-level production logger via
  `structlog.testing.capture_logs()`. Under `pytest -n 6 --dist loadscope` a
  logger warmed-and-orphaned by an earlier test on the same xdist worker —
  `cache_logger_on_first_use=True` pins a `BoundLogger` to a processor-list
  instance that a later `structlog.configure(...)` then replaces — caused
  `capture_logs` to miss the event (empty list), reddening the whole
  `Python (ruff + mypy + pytest)` job and letting the JWT-absence check pass
  vacuously against an empty capture. The test now binds a private
  `structlog.testing.LogCapture` and monkeypatches the subject module's
  `_log`, the established #1254 pattern (the sibling
  `test_operations_register_ingested.py` orphan-class assertions were
  converted the same way by #1712); the convention is documented in
  `docs/codebase/backend.md`. No production logging config change. (#1622)

### Documentation

- Fix the `meho targets import` examples in the VCF Operations / VCF Logs onboarding guides — they used an unsupported flag form, but `import` takes a `targets.yaml` **file** — and show the per-target `verify_tls` / `tls_ca_pin` TLS-trust fields for reaching self-signed / internal-CA appliances; refresh the vROps "probe fails with TLS error" troubleshooting row to name the per-target options (#1774).
- Update the connector docs for the short canonical product tokens that the connector-family realignment (#1814) shipped. `docs/architecture/connector-resolution.md` now documents the single canonical product↔impl_id identity — `target.product` is the same token `parse_connector_id` derives, the long↔short split is retired — with a historical note on the five realigned connectors. The SDDC Manager / VCF Automation / VCF Fleet / VCF Operations / Hetzner Robot onboarding guides now use the short `--product` value, `product:` field, and probe/audit output (`sddc`/`vcfa`/`fleet`/`vrops`/`hetzner`) so operators copy a working token; CLI verb trees, wrapper script names, and the vendor-reported Hetzner `robot-webservice` fingerprint are left unchanged (they are names, not the registry token) (#1818).
- Sweep the remaining invalid per-field flag forms (`--name`, `--product`, `--host`, `--secret-ref`, `--auth-model`, `--extras`) out of the connector onboarding guides — `meho targets import` takes a `targets.yaml` **file** (`import <file>`), not per-field flags, and there is no `meho targets create`/`update` verb in v0.2. The NSX, SDDC Manager, Harbor, VCF Fleet, and VCF Logs guides now show the valid `targets:` descriptor form (mirroring the VCF Operations / VCF Logs precedent in #1799), with the per-target `tls_ca_pin` / `verify_tls` self-signed-appliance note added for NSX and SDDC Manager and the SDDC `sso_realm` override moved into the descriptor (it spills into `extras`). The same sweep corrects the sibling `meho targets probe --name <slug>` / `meho targets describe --name <slug>` examples to the valid positional form (`probe <slug>` / `describe <slug>`) — both verbs take a positional `<name-or-alias>`, not a `--name` flag — across all eight onboarding guides; valid `--update` / `--json` / `--fqdn`-dispatch examples and product-specific prose are preserved (#1805 / #1862).
- Fix the remaining stale long-token `meho connector ingest --product <long>` examples in the cross-repo ingest/canary guides so a copy-pasted example no longer hits the `422 product_impl_id_mismatch` the realigned ingest contract (#1817) now enforces. `docs/cross-repo/connector-ingestion.md` (Hetzner Robot hand-authored-spec example), `docs/cross-repo/g36-fleet-canary.md`, and `docs/cross-repo/g36-vrops-canary.md` now pass the short, dispatch-canonical `--product` token (`hetzner`/`fleet`/`vrops`) that round-trips its `--impl`-derived connector_id; `--impl`/`--version` are unchanged. Completes the docs follow-up the targets-surface sweep (#1818) left out of scope. Historical/name references to the long tokens — connector class triples in `docs/codebase/connectors-*.md`, CLI verb-group names, spec filenames, the Hetzner `robot-webservice` fingerprint, and the #1817 migration notes in `docs/codebase/spec-ingestion.md` — are left unchanged (they are names, not accepted `--product` values) (#1860).
- Fix the retired `vcf-logs` target product token in `docs/cross-repo/vcf-logs-onboarding.md` — the vRLI onboarding guide the targets-surface sweep (#1818) left out of scope. `VcfLogsConnector` registers under the dispatch-canonical `product="vrli"` (#1798) and the `vcf-logs`→`vrli` alias bridge was removed by #1817, so the guide's `product: vcf-logs` target descriptors `422 product_impl_id_mismatch` on `meho targets import` and its registry-triple / probe-output references named a token the connector no longer uses. The two `targets.yaml` descriptors, the registry-triple and `--target`-carries prose, and the expected `meho targets probe --json` output now read `vrli` (round-trips `parse_connector_id("vrli-rest-9.0")`). The connector-id `vrli-rest-9.0`, `impl_id` `vrli-rest`, `vcf_logs` package / `core_ops.py` / `--catalog vcf-logs/9.0` spec names, `meho vcf-logs` CLI verb tree, `rdc-vrli` slug, `vrli.rdc.evoila.io` host, and `kv/data/vrli/...` Vault paths are left unchanged (they are names/paths, not the registry token) (#1875).

## [0.16.0] - 2026-06-15

### Added

- Targets now carry a first-class **`verify_tls`** flag
  (`NOT NULL DEFAULT true`, default-secure) on create / update / read
  across `POST` / `PATCH` / `GET /api/v1/targets` and the list
  projection. It is the storage + API + audit surface for a per-target
  TLS-verification opt-out (the dispatch wiring that consumes it lands
  in a follow-up); setting it `false` writes a durable `audit_log` row
  (`tls_verification_disabled` + before/after) and a WARN log, closing
  the prior gap where a target PATCH wrote an empty audit payload
  (#1780).
- Connector dispatch now **honors the per-target `verify_tls` flag**. A
  target with `verify_tls=false` reaches a self-signed / internal-CA
  appliance over an insecure TLS channel (a module-cached `SSLContext`
  with `check_hostname` off + `CERT_NONE`), emitting a WARN at client
  construction; a `verify_tls=true` target (the default) is built with
  **no** `verify=` argument, so the global `SSL_CERT_FILE` / chart
  trust-bundle path stays byte-identical to before. The pooled-client
  key gains a `verify_tls` dimension — `(tenant_id, id, verify_tls)` — so
  a PATCH that flips the flag is not served the stale client, while the
  `(tenant_id, id)` cross-tenant isolation prefix is unchanged. `Harbor`
  and `Gcloud` inherit the behaviour through `HttpConnector`; the
  out-of-pool k8s reachability probe and GitHub App token-exchange are
  unaffected (#1781).
- Targets now carry a first-class **`tls_ca_pin`** field (nullable PEM) —
  the **secure** supersession of `verify_tls=false`. Pin an appliance's
  CA / cert and connector dispatch trusts that CA while **keeping**
  `CERT_REQUIRED` + `check_hostname` on (`ssl.create_default_context()` +
  `load_verify_locations(cadata=...)`, the govc-thumbprint pattern), so a
  self-signed / internal-CA endpoint is reachable without weakening
  verification or adding the CA to the global bundle. A CA-pin takes
  precedence over `verify_tls=false` and the two are mutually exclusive
  (a `422` rejects setting both). The pooled-client key gains a CA-pin
  digest dimension — `(tenant_id, id, verify_tls, ca_pin_digest)` — so
  rotating a pin builds a fresh client while the cross-tenant isolation
  prefix is unchanged; the PEM is validated at the API boundary and
  set/rotate/clear is audited (`tls_ca_pinned` + before/after digest,
  never the PEM body). The `connector_tls_verify_failed` error and the
  operator docs now name CA-pin as the preferred per-target fix (#1784).
- Bulk read-class connector enable path across REST + MCP + CLI:
  `POST /api/v1/connectors/{id}/enable-reads`, the
  `meho.connector.enable_reads` MCP tool, and `meho connector
  enable-reads <id>` flip every GET/HEAD ingested op to enabled in one
  pass, leaving every write-shaped op (POST/PUT/PATCH/DELETE)
  default-deny. Tenant-scope-aware, idempotent, and audited as a
  single bulk-enable event with the count of ops enabled (#1749).
- The operation listing now flags an **enabled-but-unbacked composite**.
  `search_operations` (REST `GET /api/v1/operations/search` + the MCP
  tool) marks a composite hit `unbacked=true` with a `next_step`
  pointing at `meho connector ingest --catalog <product>/<version>`
  while the composite's L2 sub-operations are not ingested — so an
  operator/agent sees "enabled — run the catalog ingest first" instead
  of a silent dead-end (`composite_l2_missing`) at the first dispatch.
  The marker is tenant-scoped, mirrors the dispatch-time preflight's
  enable-aware check, and disappears once the catalog ingest lands the
  sub-ops; ordinary ops and fully-backed composites never carry it.
  `gh.composite.pr_status_summary` is wired; the registry generalises to
  future composites (#1757).
- Operator-console **approvals surface**: a notifications **bell + count
  badge** in the app-shell (live, fed by the session-gated SSE feed
  filtered to `op_class=approval`) opens a **modal** listing pending
  approval requests; reviewing one shows its op id, connector, proposed
  effect, requester, and created-at with **Approve / Deny** actions. The
  decisions POST to a new session-gated, CSRF-protected `/ui/approvals`
  BFF that calls the existing approval-queue service in-process
  (`list_pending` / `approve_request` / `reject_request`) — not the
  Bearer `/api/v1/approvals` routes, which a session cookie cannot
  authenticate. The self-approval invariant (#1401) is enforced both in
  the UI (Approve disabled when the reviewer is the requester unless
  `APPROVAL_ALLOW_SELF_APPROVAL`) and server-side (a forced self-approve
  is rejected 403); Deny stays allowed. Bell + modal are tenant-scoped.
  `approval.*` lifecycle events now classify as a dedicated `approval`
  broadcast class so the bell's SSE filter resolves (#1778).
- A connector dispatch that fails **TLS certificate verification** (a
  self-signed or internal-CA appliance) now returns a structured
  `connector_tls_verify_failed` result naming the host and both
  remediations — point `SSL_CERT_FILE` at a CA bundle / inject the cert
  into the chart trust-bundle (preferred, keeps verification on), or set
  `verify_tls=false` on that target as an audited per-target last resort
  (with the man-in-the-middle / credential-exposure caveat) — instead of
  the opaque `connector_error: ConnectError` that discarded the
  `[SSL: CERTIFICATE_VERIFY_FAILED]` cause. Non-TLS connection failures
  (DNS, connection-refused, timeout) are unchanged. No schema or CLI
  change (#1782).
- Operator-console **docs-corpus page** (`/ui/corpus`): pick an entitled
  doc collection (default-selected when only one is entitled), query the
  corpus, and read back the answer with its **cited chunks**. It reuses
  `POST /api/v1/search_docs` + `GET /api/v1/doc_collections` in-process
  (no new API endpoint), following the kb sibling `build_*_router()`
  pattern, with CSRF double-submit on the search POST (reusing the live
  cookie token so the un-swapped form stays in sync). Adds the sidebar
  nav entry and a dashboard surface tile (bento grid rebalanced 6→7)
  (#1777).

### Changed

- `POST /api/v1/doc_collections` now returns a `next_step` hint on its
  `201` response pointing at
  `POST /api/v1/doc_collections/{collection_key}/probe` while the new
  collection is `provisioning`. A created collection is not searchable
  until an explicit probe promotes it to `ready`, so the hint surfaces
  the `create → probe → ready` flow inline instead of leaving operators
  to discover the probe route after a confusing not-ready error on the
  first `search_docs`. Discoverability only — create still does not
  self-probe (#1756).

### Fixed

- **`meho targets import` now sets the per-target TLS-trust columns
  `verify_tls` and `tls_ca_pin`** instead of silently spilling them into
  the `extras` JSONB blob. Both keys were missing from the import
  mapper's `knownTopLevel` allow-set, so a descriptor that set
  `verify_tls: false` (or pinned a CA via `tls_ca_pin`) produced a target
  that kept its secure column defaults and still verified against the
  global bundle — a silent, security-relevant surprise. They are now
  first-class descriptor keys on both create and `--update`; the server's
  mutual-exclusivity / PEM-validation `422` surfaces through the import
  path, and genuinely-unknown keys still spill to `extras`. Completes the
  import-side wiring of per-target TLS trust (#1780 / #1784) (#1793).
- Operator-console **readiness pill now reflects real backend health on
  every page**, not just the dashboard. The sidebar-footer pill was
  stuck on yellow "starting" across all `/ui/*` surfaces because ~14
  routes hardcoded `ready=False` in their template context; only the
  dashboard computed it. The live verdict is now injected into every
  render by the shared context processor, read from a short-TTL-cached
  (`2 s`) readiness snapshot the session middleware computes from the
  same probe registry `GET /ready` uses — so a non-dashboard page shows
  green "ready" when the backend is healthy and "starting" when `/ready`
  would 503, at negligible per-render cost. The per-route literals are
  dropped; the dashboard's own fresh-probe behaviour is unchanged
  (#1776).
- Operator-console memory **create no longer 403s** under a background
  list refresh. The memory list's 60-second card poll re-used the
  page handler, which re-minted and `Set-Cookie`-d a fresh CSRF token
  on every render — rotating the cookie out from under an open create
  modal so the next submit failed the double-submit check with a
  silent `csrf_token_invalid`. The handler now sets the CSRF cookie on
  full-page loads only; polls reuse the live cookie token and leave it
  untouched, and the create modal now renders a visible error banner
  instead of swallowing a rejected submit (#1754).
- `vault.kv.*` now returns an actionable path-shape hint instead of an
  opaque `Forbidden` 403 when a caller passes a `path` that re-includes
  the mount segment (`path="secret/meho/…"` with `mount="secret"`).
  hvac addresses a secret as `v1/<mount>/data/<path>`, so the mount
  prefix would double to `v1/secret/data/secret/meho/…` and fail the
  Vault ACL indistinguishably from a real permission denial. All six KV
  ops (read / list / put / patch / versions / delete) now reject the
  mount-double-prefix before the Vault round-trip with a
  `VaultPathShapeError` naming the mount-relative form to use
  (e.g. `meho/test/federation`). A bare single-segment path equal to the
  mount name is still forwarded unchanged (#1755).
- Connector resolution now **ranks a hand-rolled class over an auto-shim**
  for the same `(product, version)` label. A stray ingest could register
  a `GenericRestConnector` auto-shim under a novel `impl_id` whose
  narrower derived version-range won the most-specific-version-match rung
  before a shipped hand-rolled connector's `priority` was ever consulted,
  shadowing it for the whole label. A new `hand_rolled_over_shim` tie-break
  rung — applied **before** the version-match step — drops every
  `GenericRestConnector` candidate whenever any non-shim candidate exists,
  so a hand-rolled class always outranks an auto-shim independent of
  version-range span or `priority`. No behaviour change when only auto-shims
  exist for a label (#1750).
- Connector ingest now **warns on a near-miss `impl_id`** instead of
  silently scaffolding a non-dispatchable shim. The covered-class check
  filtered on exact `(product, impl_id)`, so ingesting `nsx-rest-probe`
  when a hand-rolled `NsxConnector` already covered the same
  `(product, version)` under `nsx-rest` info-logged
  `connector_ingest_orphaned_class` and proceeded — the broken shim
  surfaced only ~7 min later at dispatch. The no-candidates branch now
  consults a hand-rolled sibling and, when one exists, emits a structured
  `connector_ingest_near_miss_impl_id` **warning** naming it ("did you
  mean nsx-rest?"); the reactive `unreplaced_auto_shim` dispatch error
  likewise threads and names the sibling with a re-ingest remediation. A
  genuinely novel `(product, version)` is unchanged (still info-logs and
  proceeds) (#1753).

### Documentation

- Operator guidance for reaching **self-signed / internal-CA connector
  targets**: `deploy/values-examples/README.md` gains a "Connector
  dispatch against self-signed / internal-CA targets" section framing
  the per-target `verify_tls=false` flag as the **last resort** (with the
  MITM / credential-exposure caveat and the `govc -k` / kubectl
  `insecure-skip-tls-verify` prior art), the `SSL_CERT_FILE` / chart
  trust-bundle CA-trust as the **secure** path (including the #572
  public-roots-clobber footgun), and per-target CA-pin (#1784) as the
  planned secure supersession; it documents setting `verify_tls` via the
  REST API `POST` / `PATCH /api/v1/targets` (and, since #1793, via
  `meho targets import` as a first-class descriptor key), references the
  `connector_tls_verify_failed` dispatch error (#1782), and names the two
  out-of-pool connectors (k8s probe, GitHub token-exchange) that do not
  honour the flag. `docs/architecture/connectors.md` cross-links it
  (#1783).
- Vault tenant-scoping upgrade guidance now **names the empty-prefix
  action for custom KV layouts**: operators whose secret paths do not
  match the default `secret/tenants/{tenant_id}/` mount-pinned prefix
  must set `VAULT_KV_TENANT_SCOPE_PREFIX` empty (or to their own mount
  layout) on the v0.15.0 default-on guard, else every per-tenant
  `vault.kv.*` call is denied. Spells out the explicit opt-out for
  custom layouts alongside the per-tenant migration path (#1758).

## [0.15.0] - 2026-06-13

### Added

- Doc-collection **create/import** surface across all three fronts
  (#1739): `POST /api/v1/doc_collections`, the `create_doc_collections`
  MCP tool, and `meho docs collections create` (with `--from-file`).
  Closes the validated, audited create-half gap — registering a
  collection no longer requires a raw `INSERT INTO doc_collections`. The
  create derives `tenant_id` from the JWT (never the body), validates
  `backend.type` against the search-backend registry (an unregistered
  type is a structured `422`, not a deferred probe-time `503`), maps a
  cross-scope `collection_key` collision to `409`, defaults `status` to
  `provisioning`, and writes an audit row under
  `op_id="meho.docs.collections.create"`. All three fronts are
  `tenant_admin`-gated (REST/CLI) / `tenant_admin` + `meho-docs`-gated
  (MCP). Update / delete and cross-tenant sharing remain out of scope.
- Per-tenant templated Vault ACL policies (≤3, by role) keyed on
  `{{identity.entity.metadata.tenant_id}}`: a deploy runbook
  (`docs/cross-repo/connector-vault-tenant-policy.md`) with the three
  policy bodies (`meho-tenant-read-only` / `-operator` / `-admin`), the
  entity-metadata + identity-group wiring that makes the template
  resolve, and verification commands; plus a
  `connectors/vault/tenant_identity.py` helper that maps an `Operator`
  onto the entity `tenant_id` metadata and the authoritative
  role→policy binding. Supersedes the per-operator alias recipe in
  `connector-vault-policy.md` §2 for shared-target access. OSS-only
  (no Enterprise namespaces) (#1724)
- `meho connector ingest-status <job-id> [--wait] [--json]` — poll or
  inspect an async ingest job after `meho connector ingest --no-wait`
  (or a lost waiting session), the CLI twin of the
  `meho.connector.ingest_status` MCP tool. Snapshots a `running` job
  (identity + lifecycle echo) by default, `--wait` polls to terminal;
  terminal rendering and the poll loop are shared with `meho connector
  ingest` (no duplicated lifecycle switch). The `--no-wait` hint and
  the poll-phase error guidance now name the verb. Closes the PR #1618
  gap (#1621)
- Connector DELETE surface for the zero-op registry stubs aborted
  ingests leave behind: `DELETE /api/v1/connectors/{connector_id}`
  (204, tenant_admin, always operator-tenant-scoped) and the
  `meho.connector.delete` MCP tool (optional `tenant_id`, omitted =
  built-in / global scope). Removes the scoped `operation_group` +
  `endpoint_descriptor` rows with one `meho.connector.delete` audit
  row, deregisters the triple's `GenericRestConnector` auto-shim when
  no rows remain anywhere (hand-coded classes never), warns —
  advisory, not error — when enabled operations are deleted, and
  re-ingest revives the connector from scratch (#1700)
- `work_ref` on scheduled triggers, inherited end-to-end by every
  dispatched run: `scheduled_trigger.work_ref` (migration 0043) is
  set at create time on `meho.scheduler.create` / `POST
  /api/v1/scheduler/triggers` and, when the trigger fires, the
  scheduler binds it around the dispatched agent run so the run's
  `agent_run.work_ref` and every audit row it produces carry the
  trigger's change-ticket reference (the previously-severed
  trigger → run seam). The scheduler-trigger list filters by
  `--work-ref` and surfaces it (`meho scheduler list --work-ref`,
  `meho scheduler create --work-ref`) (#1663)
- End-to-end `work_ref` change-ticket threading across the audit,
  approvals, agent-run, and runbook surfaces (the scheduler seam is the
  bullet above, #1663). A `work_ref` is captured once at the request
  boundary — bound into a `work_ref_var` `ContextVar` from the MCP
  argument, the `X-Work-Ref` header, or the dispatch seam (#1655, #1657)
  — and then persisted on, and filterable across, every downstream
  record: `audit_log.work_ref` with a `query_audit` filter, `AuditEntry`
  surfacing, a `GET /api/v1/audit/by-work-ref` route, and a
  `meho audit --work-ref` CLI filter (#1655, #1657); the approval queue
  (`approval_request.work_ref`, inherited by re-dispatched runs and
  filterable in the queue, #1717) plus an optional free-text reason on
  approve for parity with reject (#1718); the agent-run list
  (`agent_run.work_ref`, filterable, #1720); and runbooks
  (`work_ref` on `meho.runbook.start`, inherited by every per-step audit
  row, exposed on the run-list filter, #1719). One change-ticket
  reference now ties a scheduled trigger → agent run → approval →
  per-step audit trail together (#1713).
- Per-tenant Vault KV path convention and the accompanying `secret_ref`
  migration: connector secrets now live under a per-tenant
  `secret/tenants/{tenant_id}/` layout (replacing the per-`sub`
  arrangement), with `secret_ref` values rewritten to the new paths.
  This is the data-layer foundation the templated per-tenant ACL
  policies (#1724) and the default-on tenant-scope guard (#1725) build
  on; a deploy still holding secrets under the retired per-`sub` layout
  must run the migration runbook
  (`docs/cross-repo/vault-per-tenant-migration.md`) and may hold the
  guard off with `VAULT_KV_TENANT_SCOPE_PREFIX=""` until it has (#1723)

### Changed

- Document and pin the ingest tenant-scope contract across surfaces:
  REST `POST /api/v1/connectors/ingest` always writes under the calling
  operator's tenant (no `tenant_id` parameter), the MCP
  `meho.connector.ingest` tool targets the built-in / global scope when
  `tenant_id` is omitted, and re-ingesting the same spec under the
  other scope re-inserts every op as a shadow copy (scope-aware dedup,
  by design). The MCP tool description, the REST route description, and
  the registered-row `next_step` rationale now name the right surface
  per scope (#1699).
- Operator console rebrand — a new Graphite & Signal visual theme, a
  unified app-shell layout, and a standalone dev harness for iterating
  on the console UI without a full backplane behind it (#1690).

### Deprecated

- Deferred the removal of the 11 flat `runbook_*` MCP tool-name aliases
  and the `slug` template-id input alias from v0.14.0 to **v0.15.0**.
  v0.14.0 shipped with all 11 aliases still registered and callable and
  its release notes carried no removal or deferral line, so the
  deadline is moved explicitly rather than slipping silently: the
  `runbook_template_slug_field_deprecated` warning and the DEPRECATED
  wire descriptions now name v0.15.0 (the per-call
  `mcp_tool_name_deprecated` breadcrumb stays unversioned — it logs
  tool + replacement only), a `### Deprecated` erratum was added to
  the v0.14.0 section below, and the removal itself stays tracked in
  #1625 (re-scheduled to the v0.15.0 cycle). Nothing else changes:
  consumers already on `meho.runbook.<verb>` + `template_slug` are
  unaffected, and the migration recipe is unchanged — replace
  `runbook_<verb>` with `meho.runbook.<verb>` and rename `slug` →
  `template_slug` in template-verb arguments (#1612, #1702).

### Removed

- Removed the 11 deprecated flat `runbook_*` MCP tool-name aliases and
  the `slug` template-id input alias, completing the one-release
  deprecation contract opened in #1612 (v0.13.0) and deferred once from
  v0.14.0 to v0.15.0 by #1702. The runbook MCP family now serves exactly
  the 11 dotted `meho.runbook.<verb>` tools; a removed flat name returns
  the registry's standard unknown-tool error, and the template verbs
  accept `template_slug` only (`slug` is now rejected). The unused
  alias machinery (`register_deprecated_mcp_tool_alias`, the
  `deprecated_alias_for` marker, and the `mcp_tool_name_deprecated` /
  `runbook_template_slug_field_deprecated` warnings) was deleted with
  it. Migration (unchanged from #1612): replace `runbook_<verb>` with
  `meho.runbook.<verb>` and rename `slug` → `template_slug` in
  template-verb arguments. Template-verb responses still carry
  `template_slug`, so ids round-trip into `meho.runbook.start`
  unchanged. The database tables `runbook_templates` / `runbook_runs` /
  `runbook_run_step_states` are not tool names and are unaffected
  (#1612, #1702, #1625).

### Fixed

- Test suite: deflaked
  `test_retrieval_usage.py::test_route_audit_row_count_matches_total_searches`,
  which intermittently red-flaked the unit lane with `total_searches == 0`.
  Root cause was a time-bomb, not the hypothesised xdist engine-isolation
  race: the route test seeded `audit_log` rows at a fixed past date
  (`_NOW = 2026-05-14`) while the route resolves its default `since`
  window relative to the real wall clock (`now - 30d`), so the rows fell
  out of the window once the calendar advanced ~30 days past `_NOW`. The
  seed timestamps now anchor to `datetime.now(UTC)`; both assertions
  (`total_searches == 2` and `payload["row_count"] == 2`) are unchanged,
  and the fix is tests-only (no production engine/session behaviour
  change) (#1722)
- Test suite: hardened
  `test_retrieval_usage.py::test_rest_only_dogfood_zero_is_not_context_free`
  (the deferred sibling of #1722, same root cause). The test seeds
  `/api/v1/retrieve` `audit_log` rows and asserts `total_searches == 0`
  to prove REST is excluded from the counted search surfaces (#632), but
  its two seeds were pinned to the fixed `_NOW = 2026-05-14` while the
  route resolves its default `since` window against the real wall clock
  (`now - 30d`). Once the calendar advanced past `_NOW + 30d` the rows
  fell out of the window, so the zero passed by window expiry rather than
  by surface exclusion — the guard had gone vacuous (a regression that
  wrongly counted REST rows would no longer fail it). The seeds now
  anchor to `datetime.now(UTC)` so the rows stay in-window and the zero
  genuinely exercises the exclusion path; all four assertions
  (`total_searches`, `buckets`, `counted_surfaces`, `rest_excluded`) are
  unchanged, tests-only (#1734)
- Operator console: broadcast feed/wall and the connectors recent-ops
  card rendered dead (empty state despite a healthy stream, console
  errors on every SSE frame) because their Alpine component scripts
  loaded after Alpine had already started; component registration now
  loads from a head-level `component_scripts` block that precedes
  `alpine.min.js` (#1692)
- Operator console: every Memory create-modal submit silently 403'd
  (`csrf_token_invalid`) because the modal render rotates the
  `meho_csrf` cookie while the form still echoed the stale page-level
  `X-CSRF-Token`; the create form now declares its own `hx-headers`
  echo of the token minted with the modal, so the double-submit pair
  always matches and the create round-trips to 204 + redirect (#1693)
- Operator console: the Memory create form's "Leave blank to use the
  scope's default TTL" hint is now backed by the handler — a blank
  `expires_at` on a user-scope create runs through the same shared
  default-TTL resolver the REST and MCP write paths consume and
  persists `now(UTC) + MEMORY_USER_DEFAULT_TTL_DAYS` (default 7 days)
  instead of `null`, which the expiry sweeper never reaps; tenant- and
  target-scoped creates still persist no expiry, and an explicit
  timestamp is honoured verbatim (#1697)
- Alembic data backfill (`0038`) reconciles pre-v0.14.0 ingested rows
  persisted under the long VCF-family / SDDC / Hetzner-Robot product
  spellings (`vcf-logs`, `vcf-automation`, `vcf-fleet`,
  `vcf-operations`, `sddc-manager`, `hetzner-robot`) to the
  dispatch-canonical short spellings (`vrli`, `vcfa`, `fleet`,
  `vrops`, `sddc`, `hetzner`) that v0.14.0's register-time
  reconciliation (#1647) writes for new ingests — the connectors'
  pre-existing operations become dispatchable again after upgrade
  instead of reporting `registered, 0 ops`. Built-in rows only
  (`tenant_id IS NULL`); idempotent; rows whose short-spelling twin
  already exists (post-upgrade re-ingest) are left untouched (#1701)
- Operator console: the memory list's tag-autocomplete fetch wiped the
  card grid on page load because the `<datalist>` inherited the filter
  form's `hx-target="#memory-cards"` (htmx closest-wins attribute
  inheritance); the datalist now pins `hx-target="this"` so the
  `<option>` fragment lands in the datalist and the cards stay intact
  (#1695)
- Operator console: the sidebar footer (and the dashboard Deploy card)
  showed `v0.1.0-dev` on every deployed instance because the
  `app_version` Jinja global bound the static package `__version__`;
  it now binds the deployed-build label read from the same
  `CHART_VERSION` / `GIT_SHA` env metadata `GET /version` reports —
  `v0.14.0`-style on chart deploys, a 12-char commit id on bare-image
  runs, `unknown` on local runs without build metadata (#1698)
- Operator console: an expired Keycloak access token mid-session no
  longer dead-ends the UI on raw JSON `{"detail": "token_expired"}` —
  the BFF now silently refreshes the token pair inline (RFC 6749 § 6
  refresh grant, rotated one-time-use per RFC 9700 § 4.14 under a
  per-session row lock, session lifetime re-extended within the
  absolute cap), and when the refresh itself fails (revoked SSO
  session, unreachable Keycloak) HTML requests get a `302` back to
  `/ui/auth/login?return_to=<page>` with the dead cookie cleared
  instead of a JSON error page. Session and CSRF cookies are never
  rotated by a refresh, so already-open pages keep working (#1694)
- Operator console: the dashboard's "Recent activity" tray streamed
  nothing (permanent "Connecting to live feed…" placeholder) because it
  subscribed to the Bearer-only `/api/v1/feed`, which the browser's
  `EventSource` can never authenticate against (no `Authorization`
  header support — each attempt 401'd and reconnect-looped); the tray
  now subscribes to the session-gated `/ui/broadcast/stream` bridge the
  broadcast surface already uses (same cookie boundary as the page
  itself) and renders the live `BroadcastEvent` frames as
  time/principal/op/status rows through the XSS-safe Alpine sink
  pattern, capped at 50 in-DOM rows (#1696)
- `POST /api/v1/connectors/ingest` now returns the structured
  uncovered-version-label envelope (`product`, `version`, `impl_id`,
  `registered_classes[]` with each class's `supported_version_range`,
  `message`) on its 422 instead of a bare `detail` string, wiring the
  REST route to the same `build_uncovered_version_label_detail` builder
  the MCP `meho.connector.ingest` tool has shipped since #777 — so REST
  and MCP callers branch on the same stable fields and can't drift. This
  closes the last bare-string arm in the ingest route's typed-exception
  table, completing the #1610 400-family parity (#1624)
- Knowledge corpus federation: the `corpus-http` backend adapter now
  speaks MEHO.Knowledge's actual `/search` contract. The first real
  in-cluster `search_docs` round-trip returned **zero hits from a
  populated corpus** because the adapter read top-level
  `chunks`/`content`/`source_url` while the corpus returns
  `results`/`text`/`source_uri`, sent `limit` (which the corpus silently
  ignores) instead of `top_k`, and probed a derived `/status` URL the
  corpus does not expose (it serves `/readyz`). The adapter now maps all
  four mismatches via `AliasChoices`, and — the load-bearing safety fix
  — a successful 2xx body naming neither envelope raises
  `CorpusUnavailable` (→ 503) instead of silently parsing an empty list,
  guarded by a regression test. The corpus wire contract and the
  single-operator agent-requester approval story are now documented for
  corpus-http implementers (#1732, #1737, #1738)
- CLI: an operation parked for human approval
  (`OperationResult.status="awaiting_approval"`) now renders as a
  non-error, **exit-0** parked outcome on every dispatch path — hoisted
  into the shared `dispatch.Render` rather than re-implemented per verb
  — with the hint `parked for human approval — approve via the approval
  queue, then re-dispatch`; `--json` emits the full envelope (incl.
  `extras.approval_request_id`) and also exits 0. Previously the
  hardcoded `ok`/`error`/`denied` allowlist rejected `awaiting_approval`
  as an error. The MCP `call_operation` `outputSchema` status enum now
  includes `awaiting_approval` (#1740)
- Test suite: pinned an empty `VAULT_KV_TENANT_SCOPE_PREFIX` in the
  credential-dispatch fixtures so they deterministically exercise the
  pre-guard path now that #1725 flipped the tenant-scope guard
  default-on (tests-only) (#1742)

### Security

- The #1643 Vault tenant-scope guard is now **default-on**: agent-supplied
  `vault.kv.*` calls are confined to the calling operator's tenant
  namespace out of the box, no per-deploy opt-in. The default
  `VAULT_KV_TENANT_SCOPE_PREFIX` is the **mount-pinned**
  `secret/tenants/{tenant_id}/` — the mount segment is required because the
  guard matches a normalised `<mount>/<path>` candidate on the default
  `secret` KV mount, so a path-only `tenants/{tenant_id}/` would deny every
  legitimate per-tenant call. Builds on the per-tenant layout (#1723) and
  templated policies (#1724). The startup advisory inverts accordingly:
  silent on the default, firing only when an operator explicitly sets the
  prefix back to empty. The platform's own federation-proof health read
  (`secret/meho/test/federation`, `GET /api/v1/health`) is exempt via a
  closed allow-list so the default-on guard does not deny it; the exemption
  is scoped to **read-only** verbs, so a `put`/`patch`/`delete` to that
  shared platform path under a non-owning operator stays tenant-scoped. A
  malformed `VAULT_KV_TENANT_SCOPE_PREFIX` (missing the `{tenant_id}`
  placeholder, unbalanced braces, or an extra placeholder) is now rejected
  at startup rather than failing at first `vault.kv.*` call. A deploy still
  holding secrets under the retired per-`sub` layout disables the guard with
  `VAULT_KV_TENANT_SCOPE_PREFIX=""` until the migration runbook
  (`docs/cross-repo/vault-per-tenant-migration.md`) has run (#1725)

## [0.14.0] - 2026-06-12

### Security

- Closed a cross-tenant IDOR on the scheduler
  (`GET`/`POST`/`DELETE /api/v1/scheduler/triggers`) and retrieval
  (`GET /api/v1/retrieve/usage`, `POST /api/v1/retrieve/retire-checklist`)
  routes: a caller-supplied `tenant_id` / `tenant_filter` was authorized on
  `tenant_admin` **rank** alone, so a tenant-admin of tenant A could
  read/act on tenant B. A new shared `authorize_tenant_scope` helper now
  requires the cross-tenant `platform_admin` capability (#1638) to target
  another tenant; requesting one's own tenant (or omitting the filter) is
  unchanged. The 403 detail token changes from
  `tenant_filter_requires_tenant_admin` to
  `cross_tenant_requires_platform_admin` (#1640).
- Added a defense-in-depth tenant-scope guard on the agent-supplied
  `vault.kv.*` ops (`read` / `list` / `versions` / `put` / `patch` /
  `delete`): the requested `mount`/`path` is now checked against the
  operator's tenant namespace **before** the hvac call, so a tenant-A
  caller reaching for a tenant-B path is denied with a structured
  `connector_error` (`exception_class=VaultTenantScopeError`) even if the
  shared Vault `meho-mcp` policy is mis-provisioned too broadly. The guard
  is **opt-in** via `VAULT_KV_TENANT_SCOPE_PREFIX` (a `{tenant_id}`
  format template, e.g. `tenant-{tenant_id}/`); empty (the default) leaves
  behaviour unchanged because the shipped Vault layout scopes per operator
  `sub`, not per tenant. The convention is documented in
  `docs/codebase/connectors-vault-tenant-scope.md` (#1643).
- Closed a cross-tenant enumeration hole on the MCP `list_targets` tool:
  the caller-supplied `tenant_id` / `tenant` argument was resolved with
  no equality check and gated on `tenant_admin` **rank** alone, so a
  tenant-admin of tenant A could enumerate tenant B's targets. Cross-
  tenant listing now requires the `platform_admin` capability (#1638);
  naming one's own tenant (by slug or UUID) or omitting the argument is
  unchanged, and an unauthorized cross-tenant request surfaces as the
  MCP `-32602` (INVALID_PARAMS) error (#1641).
- Re-keyed the VCF (vROps / vRLI / Fleet), Harbor, NSX, and SDDC-Manager
  connectors' per-target credential and session-token caches on the
  tenant-unique `(tenant_id, target.id)` tuple instead of `target.name`.
  Two same-named targets in different tenants previously collapsed onto one
  cache entry, so one tenant could be served another tenant's cached
  service-account credential or session token (#1642).
- Extended the same `(tenant_id, target.id)` cache re-keying (via the shared
  `target_cache_key` helper) to the remaining seven connectors that still
  keyed credential/session/token caches on `target.name`: VCF Automation
  (both per-plane token caches), ArgoCD, gcloud (token + impersonated-creds +
  per-target lock), Keycloak (admin token), GitHub (installation token + PAT),
  Hetzner Robot, and vmware-rest (session token + endpoint paths). Two
  same-named targets in different tenants no longer collapse onto one cache
  entry, closing the same cross-tenant credential/session bleed in these
  connectors. (The shared `HttpConnector._clients` connection pool was the
  one remaining `target.name`-keyed cache; it is re-keyed in the following
  bullet, #1682.) (#1672).
- Re-keyed the shared connection pools — `HttpConnector._clients`
  (every HTTP connector) and `SshConnector._connections` (bind9, pfSense,
  Holodeck) — on the tenant-unique `(tenant_id, target.id)` tuple
  (`target_cache_key`) instead of `target.name`, closing the pooling
  concern deferred in #1642/#1672. Each pooled `httpx.AsyncClient` is
  host-bound via `base_url` (and each SSH connection is bound to a live
  host session), so when two tenants legitimately owned same-named targets
  pointing at different hosts, the name-keyed pool served tenant B's
  request through tenant A's host-bound client — a cross-tenant request
  **misroute** and credential leak below the authz layer. The vmware-rest
  `aclose()` session-revoke path and the NSX cookie-jar invalidation, which
  reached the pool directly by name, now resolve clients by the same
  tenant-unique key (the vmware-rest `_session_names` name reverse-map is
  removed as redundant). Same-tenant pooling behaviour is unchanged (#1682).

### Breaking changes

- `meho connector edit-op --enable` no longer reports a silent
  `ok` on an op whose resolved connector is the unconfigured
  spec-ingest `GenericRestConnector` auto-shim: the CLI prints
  `warning (unreplaced_auto_shim): ...` to stderr naming the missing
  per-product Connector subclass (and that re-ingesting the spec will
  not replace the shim), the REST route returns the same advisory as
  a structured `warnings[]` field, and the `meho.connector.edit_op`
  MCP tool mirrors it — closing the dead-end remediation chain where
  `composite_l2_disabled` pointed at an enable that succeeded and
  then dispatch failed one layer deeper with `connector_unsupported`
  / `cause=unreplaced_auto_shim` (#1627's dispatch-time error; this
  is its proactive enable-time counterpart). The enable still
  applies — warnings never block the write. **Wire change:** to
  carry the advisory, `PATCH
  /api/v1/connectors/{id}/operations/{op_id}` now returns `200` with
  an `EditOpResponse` body (`{"warnings": [...]}`) instead of `204
  No Content`. Migration: clients asserting `status == 204` accept
  `200` (and may read `warnings`); clients generated from
  `cli/api/openapi.json` regenerate against the refreshed snapshot —
  the bundled `meho` CLI in this release already is. (#1630)

### Added

- A read-only **dispatch request preview** — `POST
  /api/v1/operations/preview`, the MCP `preview_operation` tool, and the
  regenerated CLI client — resolves an ingested op + params to the literal
  would-be HTTP request (`method` + substituted `path` + `query` +
  **redacted** `body`) and **returns** it instead of dispatching. It makes
  a write failure self-diagnosable from the inside: an operator who hit a
  gh-rest write `422` / `403` re-issues the same arguments against
  `/preview` to read back exactly what would be put on the wire, rather
  than bisecting payload shapes from the outside — the operation audit
  persists only a hashed `params_hash`, so the request shape is otherwise
  unrecoverable. The body is redacted through the **same**
  connector-boundary pipeline the response path uses (a bearer token in a
  body value is masked just as in a response), so it is request-time
  observability, **not** a new persisted-secret surface: nothing is
  written to the audit row and the `params_hash` privacy choice is
  untouched. The literal request is resolved through the **same** code
  path `dispatch_ingested` sends through (path substitution, `mount_op_path`
  prefix, requestBody unwrap), so the preview can never drift from the real
  request. `typed` / `composite` ops return `status="unavailable"` (no
  single literal HTTP request to preview); inspection only — it never sends
  the request and never re-dispatches a past one (replay is out of scope).
  The observability counterpart to #1656 (requestBody unwrap) and #1649
  (structured `403`/`422` shape) (`claude-rdc-hetzner-dc#1138`) (#1683).
- `Operator` now carries a `platform_admin: bool` flag, parsed from a
  configurable JWT claim (`JWT_PLATFORM_ADMIN_CLAIM_NAME`, default
  `platform_admin`) and defaulting to `False` when the claim is absent or
  malformed. The flag is **orthogonal** to `TenantRole` (which is scoped
  *within* a tenant) and marks a genuine cross-tenant *platform*
  operator. It is fail-closed — every existing token, and every agent /
  service principal, materialises as non-platform-admin unless a realm
  explicitly grants the claim — and no surface consumes it yet: it is the
  substrate a later cross-tenant authorization gate checks, so a
  `tenant_admin` is never mistaken for a platform operator on role rank
  alone (#1638).
- `GET /api/v1/connectors` rows now split the operation rollup
  enabled-vs-total: `enabled_operation_count` (ops whose per-op
  `is_enabled` dispatchability flag is set) lands next to the
  existing `operation_count` total, mirroring the `*_group_count`
  family's naming, so an operator (or an LLM browsing the catalog)
  can tell how many of a connector's operations are actually
  dispatchable vs ingested-but-disabled (`vmware-rest-9.0`: ~2,211
  ingested, only a fraction enabled). The `meho.connector.list` MCP
  tool returns the same rows. Additive — existing `operation_count`
  consumers are unaffected. (#1636)
- The backplane now emits a single structured startup advisory
  (`vault_tenant_scope_unenforced`) when the opt-in Vault `vault.kv.*`
  tenant-scope guard (#1643) is left default-off
  (`VAULT_KV_TENANT_SCOPE_PREFIX` unset), so an operator running a
  tenant-partitioned Vault has a signal that cross-tenant `vault.kv.*`
  isolation is unenforced at the app layer instead of the guard silently
  no-op'ing. The advisory names the enabling env var and the doc; it is
  observability-only — dispatch behaviour and the empty default are
  unchanged (flipping the guard on is an explicit infra decision). A new
  "Choosing a layout" section in
  `docs/codebase/connectors-vault-tenant-scope.md` documents the
  per-`sub` vs tenant-partitioned choice and what enabling the prefix
  requires (#1673).
- The manual `--spec` connector-ingest path now accepts an
  operator-supplied `spec_info_versions_compatible` band (REST body
  field + `meho connector ingest --spec-info-versions-compatible`,
  repeatable or comma-separated), mirroring the catalog opt-in (#1307).
  A vendor spec that self-versions independently of the connector's
  product-line label — e.g. the version-stable vRLI `/api/v2` surface
  reporting `info.version="v2"` while the seeded `VcfLogsConnector`
  label is `9.0` — now ingests under `--version 9.0
  --spec-info-versions-compatible 2.x` instead of failing the
  spec/label cross-check; omitting the band keeps the strict check, and
  a non-pattern token (a bare `v2`) is rejected at request validation.
  (#1646; consumer signal claude-rdc-hetzner-dc#1136)

### Deprecated

- *Erratum — added 2026-06-12, after the v0.14.0 tag (#1702).* Deferral
  of flat `runbook_*` alias removal to v0.15.0 (originally scheduled
  per the #1612 migration recipe announced in v0.13.0; execution
  deferred to the next release). v0.14.0 ships with the 11 flat
  `runbook_*` MCP tool names and the `slug` template-id input alias
  still callable as deprecated aliases; they are removed in v0.15.0,
  tracked in #1625 (re-scheduled to the v0.15.0 cycle). Consumers who
  already migrated to `meho.runbook.<verb>` + `template_slug` are
  unaffected; consumers still on the flat names keep working through
  v0.14.x and must migrate before v0.15.0.

### Fixed

- The chart CI lane's `helm-test` job no longer intermittently fails its local
  backplane image build on HuggingFace's anonymous per-IP `429 Too Many
  Requests`. The model-bake layer (`RUN python -m meho_backplane.retrieval.warm`)
  re-downloaded the default embedding model on effectively every run — the
  BuildKit layer cache misses because per-build `BUILD_DATE`/`GIT_SHA` args and
  per-commit venv content invalidate the warm layer — and GitHub-hosted runners
  share egress IPs, so the anonymous HF quota tripped at random. The job now
  restores the unit lane's existing fastembed `actions/cache` entry (same key),
  stages the snapshot into the gitignored `backend/.model-preseed/`, and the
  Dockerfile `COPY`s it into `/opt/meho/model-cache` ahead of the warm `RUN`;
  fastembed's local-first probe then finds the snapshot and performs zero
  HuggingFace requests on a warm build. A cold cache (or a local
  `docker build backend/` with no pre-seed) downloads exactly once, as before,
  and the warm step still runs the #574 drift-guard assertion (real embed +
  `EMBEDDING_DIMENSION` check) in every path (#1623).
- An async `--spec` ingest no longer false-succeeds when nothing became
  dispatchable, and the `--product` the catalog's `next_step` verb prints
  now round-trips. Two tangled defects: (1) ingesting under a VCF-family
  **long** product (`--product vcf-logs`) persisted `endpoint_descriptor`
  rows the dispatch/query surface never queried — it keys on the **short**
  product `parse_connector_id` derives from the connector_id (`vrli`), so
  the catalog reported `registered, 0 ops` and `search_operations`
  returned `connector_not_ingested`. Ingest now reconciles the row product
  to the dispatch-canonical spelling at register-time (all six splits:
  `hetzner-robot/hetzner`, `sddc-manager/sddc`, `vcf-automation/vcfa`,
  `vcf-fleet/fleet`, `vcf-logs/vrli`, `vcf-operations/vrops`; a no-op for
  aligned connectors), and a registered-but-unpopulated row's `next_step`
  verb emits the **registry** `--product` (e.g. `vcf-logs`) so the
  operator's ingest finds the real connector class and runs a real
  version-coverage pre-flight — the same register-time reconciliation
  then lands the rows dispatchably under the short product, so the verb
  still round-trips. (2) The async job flipped to `succeeded` purely
  because the pipeline coroutine returned; it now consults a
  dispatchability probe (the connector resolves under its dispatch key)
  and ends `degraded` with `error_class="ingested_not_dispatchable"` when
  the run is genuinely non-dispatchable — never a bare `succeeded` over a
  connector that persisted nothing callable. A benign idempotent re-run
  (every op skipped, so `inserted_count == 0`, but the connector is
  already dispatchable) stays `succeeded`, so a no-op re-ingest no longer
  reads as a failure. New `degraded` job status surfaces on the REST/MCP
  poll response and the CLI renders it as a non-zero failure (including
  under `--json`). Diagnoses the v0.13.0 vcf-logs log-sentry
  false-success; see claude-rdc-hetzner-dc#1136. (#1647)
- An ingested **L2** write op no longer mangles its HTTP request body: the
  dispatcher now serializes the single `x-meho-param-loc: "body"` container
  param's *value* as the JSON body (unwrapped) on every body-carrying arm,
  instead of wrapping it under the param name (`{"body": {…}}`). Every
  ingested-L2 REST write (gh-rest issue-create / issue-comment / add-labels /
  create-PR, …) previously 422'd because the upstream saw the requestBody
  schema nested one level too deep; a gh-rest issue-create with
  `body: {"title": "X"}` now sends exactly `{"title": "X"}` on the wire.
  Generic to all ingested-L2 connectors with a requestBody; reads (no body)
  and path/query/header routing are unchanged. Diagnoses the RDC log-sentry
  issue-filing finding (`gh api` 201 vs meho 422 on identical `{title}`);
  see claude-rdc-hetzner-dc#1138. (#1656)
- A failed park-time `proposed_effect` preview no longer degrades
  silently to the identifier-only default: the parked approval now
  carries `preview_unavailable: true` plus a `preview_error` reason
  alongside the identifier fields (visible on REST
  `GET /api/v1/approvals`, `meho.approvals.list` / `.get`, and `meho
  approvals show`), so a four-eyes reviewer can tell "blast-radius
  unknown" from a genuinely small action when a `vmware.composite.*`
  preview's listing read cannot execute. The park itself still always
  proceeds; successful previews are unchanged. (#1628)
- A reduced result whose rows exceed the inline sample but could not be
  spilled to the read-back store no longer fails silently: the handle's
  `fetch_more.drill_in` now carries a machine-readable `reason`
  (`no_tenant_context` / `result_store_unavailable`) next to a
  reason-specific rationale, and every skipped spill logs a structured
  `jsonflux_spill_skipped` warning. Diagnoses the RDC cycle-8
  `k8s.logs tail=300` 5-of-300-sample finding — not a #1507 regression
  and not a k8s.logs-shape gap (pinned by repro tests); see
  `docs/codebase/result-spill.md` for the triage runbook. (#1629)
- A connector raising `NotImplementedError` on dispatch now returns a
  structured `connector_unsupported` error instead of the opaque
  `connector_error: NotImplementedError` that buried the descriptive
  raise-site message in `extras.exception_message`. The message is
  promoted verbatim into the operator-facing `error` string and
  `extras.detail`, and `extras.cause` distinguishes
  `unsupported_feature` (e.g. a target `auth_model` the connector
  doesn't support — fix the target config) from `unreplaced_auto_shim`
  (the resolved connector is the spec-ingest auto-shim — register the
  per-product Connector subclass), each with its remediation and doc
  reference in the message. Reaches both the REST dispatch response
  and the MCP `call_operation` tool, matching the `composite_l2_*`
  envelope parity (#1627).
- An upstream **403 Forbidden** or **422 Unprocessable Entity** on a
  write dispatch (e.g. a gh-rest `POST /repos/{owner}/{repo}/issues`)
  now returns a structured `connector_http_403` / `connector_http_422`
  error instead of the opaque `connector_error: HTTPStatusError` that
  surfaced only httpx's status line and buried GitHub's actionable body
  in `extras.exception_message`. Both causes are named
  **connector-agnostically**: a 403 is an insufficient-permission
  rejection (the backing credential — e.g. a GitHub App with
  `issues: read` but not `issues: write` — authenticated but may lack
  the op's required scope, a target-credential matter, not a meho
  transport fault), with `extras` carrying `http_status: 403`, the
  upstream `upstream_message`, and any GitHub permission headers
  (`permission_headers`, `X-Accepted-GitHub-Permissions` /
  `x-oauth-scopes`) the upstream sent; a 422 is an invalid-payload
  rejection (the upstream parsed the request but rejected its content),
  with `extras` carrying `http_status: 422`, the upstream
  `upstream_message`, and the GitHub-style `validation_errors` (the
  body's `errors[]` field-level array) when present — the detail that
  slowed the diagnosis of the gh-rest write-body bug. Scoped to 403 +
  422 — every other `HTTPStatusError` status still flattens to
  `connector_error` unchanged. Extends #1627's dispatch structured-cause
  pattern to the transport-error sibling; reaches both the REST dispatch
  response and the MCP `call_operation` tool
  (`claude-rdc-hetzner-dc#1138`) (#1649).
- `meho connector list --json` no longer silently drops the `state`,
  `next_step` and `enabled_operation_count` fields the backend ships on
  every `GET /api/v1/connectors` row — the machine surface was
  advertising an incomplete row shape, so scripts and LLM consumers
  could not tell a dispatchable (`ingested`) connector from a
  registered-but-empty one, see the self-describing remediation verb
  for half-registered connectors, or read the enabled-vs-total
  operation split. The CLI's decode shape now mirrors all 13
  `ConnectorListItem` fields and the canonical wire-shape test rejects
  unknown fixture keys so the mirror cannot silently regress. The
  human table is unchanged. (#1645)
- `list_operation_groups` no longer hides a group that holds live ops just
  because the group's own review is still `staged`. Group listing keyed off
  the group's `review_status='enabled'` while `search_operations` + dispatch
  key off per-op `is_enabled`, so a connector whose ops were made live one
  at a time via `meho connector edit-op … --enable` (the scope-minimal path,
  since `meho.connector.enable` cascades to **every** op) showed zero groups
  to the discovery tool whose own description says to "call this FIRST."
  Such a group is now surfaced, flagged `partial=true` with a non-zero
  `enabled_op_count`, so groups-first discovery stays in sync with what is
  actually dispatchable; a staged group with zero enabled ops still stays
  hidden, and a fully-enabled group is returned without the marker. The
  group-level cascade semantics of `meho.connector.enable` are unchanged
  (#1648 — consumer signal claude-rdc-hetzner-dc#1136).

## [0.13.0] - 2026-06-11

### Added

- Parked `vmware.composite.*` write approvals now carry a
  connector-rendered preview in `proposed_effect` instead of the
  identifier-only `{op_id, connector_id, target_id}` default, extending
  the #1504/#1437 park-time preview pattern to all 8 vmware write
  composites. The fan-out composites (`vm.power.bulk`, `host.evacuate`,
  `host.detach_from_vds`, `cluster.patch`) resolve the entity set the
  approved dispatch would act on — via the same read-only listing
  helpers the handlers use — and store the requested action/filter plus
  a capped `resolved` list with `total_resolved`, so a four-eyes
  reviewer can tell a one-VM power cycle from a 1000-VM outage; the
  single-entity composites (`vm.create`, `vm.clone`,
  `vm.snapshot.revert`, `vm.migrate`) echo their blast-radius-naming
  params. Preview reads are GET-only by construction and fail-soft —
  the park always proceeds (#1608).
- `GET /api/v1/runbooks/templates` and `GET /api/v1/runbooks/runs` now
  honour the `?envelope=v2` opt-in and return the unified
  `{items, next_cursor}` list shape (api-shape-conventions §2), joining
  the seven sibling list endpoints widened in #1312/#1356. Both listings
  are unpaged, so `next_cursor` is always `null`; omitting the param
  keeps the keyed `{"templates": [...]}` / `{"runs": [...]}` defaults
  unchanged (#1611).

### Deprecated

- The 11 flat `runbook_*` MCP tool names (`runbook_start`,
  `runbook_show_template`, …) are deprecated in favour of dotted
  `meho.runbook.<verb>` canonical names, joining the
  `meho.<noun>.<verb>` grammar every other multi-verb tool family
  already uses; the template id is now `template_slug` on all 11 tools
  (previously `slug` on the template verbs vs `template_slug` on the
  run verbs), so an id returned by `meho.runbook.show_template` /
  `.list_templates` is accepted by `meho.runbook.start` verbatim. The
  flat names and the `slug` input field stay callable as deprecated
  aliases for one release — identical handlers and schemas, DEPRECATED
  wire descriptions, structured `mcp_tool_name_deprecated` /
  `runbook_template_slug_field_deprecated` warning logs per use — and
  are removed in v0.14.0. Migration recipe: replace `runbook_<verb>`
  with `meho.runbook.<verb>` and rename `slug` → `template_slug` in
  template-verb arguments (#1612).

### Fixed

- `/ready` no longer fail-closes on the self-registered `corpus-http`
  docs backend when `CORPUS_URL` is unset. The docs add-on is optional:
  the coarse `docs_backends` readiness check now skips unconfigured
  backends (registered ≠ configured), so a deploy with no docs backend
  configured becomes Ready instead of returning 503 forever (which made
  `helm --wait` time out and the rollout never complete). Call-time
  behaviour is unchanged — `search_docs` still fails closed with 503
  `CorpusUnavailable` when the corpus is unconfigured or unreachable
  (#1606).
- Distinguish ingested-but-**disabled** L2 sub-ops from truly-absent ones
  in the `vmware-rest` composite pre-flight. A composite that depends on
  an L2 op whose descriptor row exists but is `is_enabled=false` now
  returns a new `composite_l2_disabled` error (with `disabled_op_ids[]` +
  `connector_id`) whose remediation names a real verb —
  `meho connector edit-op <connector_id> <op_id> --enable` — instead of
  the wrong `composite_l2_missing` / re-ingest hint. Truly-absent ops are
  unchanged. On a default `vmware-rest-9.0` deploy (L2 surface
  ingested-but-disabled) this stops every composite read from steering
  operators to re-run an ingest that already happened (#1601).
- Reconcile the `vmware.composite.network.portgroup.audit` composite's L2
  `op_id` keys with the canonical vCenter REST Automation surface so the
  composite is dispatchable on real deploys. The composite declared
  singular keys — `GET:/vcenter/network/distributed-switch` and
  `GET:/vcenter/network/distributed-portgroup` — that resolve against no
  operation in the ingested vCenter spec (neither path is a real
  resource), so the composite silently failed to dispatch its L2 legs.
  The keys are corrected to the real resources: the plural
  `GET:/vcenter/network/distributed-switches` for the DVS leg and the
  generic `GET:/vcenter/network` (filtered to `DISTRIBUTED_PORTGROUP`,
  since distributed portgroups have no dedicated list resource) for the
  portgroup leg, with best-effort degradation when the DVS leg is
  unavailable. A build-time guard test asserts every declared composite
  `op_id` resolves against the ingested spec so a future drift fails CI
  rather than at runtime (#1602 / #1603).
- Defuse the pre-upgrade-migration ↔ auto-rollback trap that made
  `helm --atomic` dead on arrival for any release carrying a migration
  (live ~2.5h outage, 2026-06-08): the `db` readiness probe demanded
  strict `current == head` revision equality, so once the
  `pre-install,pre-upgrade` Job committed the new migration — a side
  effect `helm rollback` never reverts — both the still-running prior
  pods and any rolled-back pods failed `/ready` forever
  (`current=0037 head=0036`). The probe now tolerates a database
  **ahead** of the image's head (revision unknown to the image's
  `versions/` directory ⇒ stamped by a newer release), reporting
  `ok=true` with `db_ahead=true` in the detail; this leans on the
  CI-enforced additive-only `upgrade()` contract that already
  guarantees older code reads newer schemas. A DB *behind* head (missed
  migration) still fails readiness, as does a missing pgvector
  extension. The migration ↔ rollback contract, the rejected
  `pre-rollback` `alembic downgrade` alternative (Helm renders rollback
  hooks from the *previous* release's manifests — the old image lacks
  the newer migration scripts), and a failure-injection rollback drill
  are documented in `docs/codebase/migrations.md` and
  `docs/RELEASING.md` § 6b (#1607).
- `meho connector ingest` no longer dies with a fatal
  `unexpected_response` when a v0.12+ backplane answers the default
  async shape (`202 Accepted` + ingest-job handle) — an error that hid
  a successfully started job and baited operators into retrying, i.e.
  double-ingesting. The CLI now treats 202 as success: it polls
  `GET /api/v1/connectors/ingest/jobs/{job_id}` to a terminal status by
  default (rendering the same summary / `--json` shape as the
  synchronous path, with token refresh kept alive across long waits)
  and a new `--no-wait` flag exits 0 with the job handle instead. A
  failed job surfaces its `error_class` + message (exit 4), and a job
  lost to a backplane restart tells the operator to check
  `meho connector list` before re-running. Legacy synchronous `200`
  responses (and `--dry-run`, which always runs inline) are unchanged
  (#1609).
- The REST `POST /api/v1/connectors/ingest` route now returns the same
  structured detail envelopes for the five typed parser-family
  `SpecError` rejections (`unsupported_spec` / `invalid_spec` /
  `invalid_schema` / `op_id_collision` / `llm_output_invalid`) that the
  MCP ingest tool has shipped on `error.data` since #1534, instead of
  collapsing them to a bare `400` string. A Swagger 2.0 spec now yields
  `detail.detail == "unsupported_spec"` with the actionable
  swagger2openapi / converter.swagger.io conversion remediation in
  `detail.message`, so REST/SDK callers branch on the stable classifier
  instead of re-parsing prose; the human-readable message is carried
  verbatim inside the envelope (#1610).
- SSE disconnect-path audit writes are no longer dropped silently under
  a second cancellation. On a client disconnect `AuditMiddleware`
  catches `CancelledError` and writes the audit row before re-raising
  (#1389), but that write was **unshielded** — a second `CancelledError`
  arriving mid-INSERT (task-tree teardown, server shutdown, an enclosing
  `timeout()`/anyio cancel scope) propagated out of the bare `await`,
  bypassed `_finalize`'s `except Exception` arm (`CancelledError` is a
  `BaseException`), and dropped the row with no log line — a silent hole
  in the "every authenticated action gets exactly one row" fail-closed
  contract. The disconnect-path write is now scheduled as a task and
  drained to completion under `asyncio.shield`, so redelivered
  cancellations interrupt only the wait, never the shielded write; the
  original `CancelledError` is re-raised once the row commits, leaving
  enclosing `TaskGroup`/`timeout()` semantics unchanged. Only the
  `CancelledError` arm is shielded; the normal return path is unchanged
  (#1600).

## [0.12.0] - 2026-06-08

### Added

- Add a corpus-agnostic per-tenant **capability gate** on the MCP tool +
  resource surface (G4.5-T1). A `ToolDefinition` /
  `ResourceTemplateDefinition` may now declare an optional
  `required_capability`; a tool/template carrying one is **absent** from
  `tools/list` / `resources/templates/list` AND rejected with a
  403-class error at `tools/call` / `resources/read` for any operator
  whose tenant hasn't provisioned that capability — true absence, not
  just un-callable, so an agent never sees a capability it can't use.
  The gate is a second axis orthogonal to the existing role gate
  (mirrors the connector enable model, not a packaging/entitlement
  system). `Operator` gains a `capabilities: frozenset[str]` populated
  from a configurable JWT claim (`JWT_CAPABILITIES_CLAIM_NAME`, default
  `capabilities`) with no DB hit on `tools/list`; an absent or malformed
  claim resolves to the empty set (fail-closed). `meho://tenant/{id}/info`
  now returns a `capabilities` array so MCP clients and the CLI read
  provisioning from one source of truth. The `meho-docs` add-on is the
  first consumer (#1528).
- Backplane→corpus federation client for the `meho-docs` add-on: an
  async client that forwards the operator JWT to the external
  vendor-document corpus over HTTP, with `CORPUS_URL` / `CORPUS_AUDIENCE`
  / `CORPUS_TIMEOUT_SECONDS` / `CORPUS_REQUIRE_FILTERS` settings and a
  fail-closed `CorpusUnavailable` error (corpus unconfigured, unreachable,
  or non-2xx) that the upcoming `search_docs` route maps to HTTP 503
  (#1520). Transport only — the `search_docs` route lands separately.
- `POST /api/v1/search_docs` — the federated vendor-document retrieval
  route of the `meho-docs` add-on (G4.5-T3). Operator role minimum,
  tenant-scoped via the forwarded operator JWT. Enforces a **mandatory
  binary product+version scope** (REQUIRE_FILTERS): a request missing
  either is rejected `422` (fail-closed), never forwarded as an
  unfiltered corpus query — the scope is a containment filter, not a
  ranking weight (#1178 / #1177). Enforcement is gated by
  `CORPUS_REQUIRE_FILTERS` (default on). The route federates to the
  external corpus via the T2 client (`CorpusUnavailable` → `503`, never
  an empty `200`) and binds one central audit row per query under the
  named op `meho.docs.search` (`op_class=read`), storing the query only
  as a SHA-256 hash plus the product/version scope and hit count — so
  `query_audit` / who-touched surface every docs query without leaking
  the raw query. The scope-validation + corpus-call + cited-chunk shape
  live in a shared `docs_search` service the future MCP tool (T4) and
  CLI verb (T5) reuse (#1521).
- `search_docs` MCP tool + `meho://docs/{product}/{version}/{chunk_id}`
  companion resource — the agent-facing face of the `meho-docs` add-on
  (G4.5-T4). Both are gated by `required_capability="meho-docs"` (T1):
  absent from `tools/list` / `resources/templates/list` for a tenant
  without the add-on and 403-class on call, present and callable once
  provisioned. The tool takes `query` + the **mandatory** `product` +
  `version` binary scope (strict 2020-12 `inputSchema`,
  `additionalProperties:false`) and federates through the shared
  `docs_search` service (T3) to the external corpus, returning ranked
  cited chunks; a missing/blank scope surfaces the REQUIRE_FILTERS
  rejection as an MCP `-32602`, a down corpus as `-32603`. Its
  description routes the agent — `search_docs` for vendor reference,
  `search_knowledge` for how-we-do-X, `search_memory` for cross-session
  state — and points at the companion resource, which recovers the full
  text of a cited chunk on a later turn by re-issuing a scoped search
  (the corpus transport is search-only). One hashed audit row per call
  (`op_class=read`); the raw query is never logged (#1523).
- `meho docs search <query> --product <p> --version <v> [--limit N]
  [--json]` — the operator-facing CLI verb of the `meho-docs` add-on
  (G4.5-T5). Wraps `POST /api/v1/search_docs` via the shared generated
  authed client (bearer + 401-refresh), mirrors the route's
  REQUIRE_FILTERS gate client-side (missing `--product`/`--version` is
  rejected before the round-trip), and renders cited chunks as a text
  table or raw JSON. The `meho docs` tree compiles into every binary
  but is gated on the tenant's `meho-docs` capability (read from the
  bearer JWT's `capabilities` claim, T1): when unprovisioned the tree
  is **hidden from `meho --help`** and every verb refuses with a typed
  `addon_not_provisioned` error before any network call — true absence,
  fail-closed. The claim is read unverified (a visibility affordance —
  the backplane and corpus federation enforce the real boundary), so a
  forged claim changes only what the CLI shows, not what the server
  allows (#1524).
- `ask_docs` MCP tool — the synthesized, **cited** answer over the
  `meho-docs` corpus (G4.5-T7), the fast-follow to `search_docs`. Runs the
  **same** shared `docs_search` retrieval (same `required_capability=
  "meho-docs"` gate, same mandatory `product`+`version` REQUIRE_FILTERS
  scope, same hashed audit row, `op_class=read`), then composes one
  grounded answer over the retrieved chunks and returns `{answer,
  citations[]}` where every citation resolves to a retrieved chunk — no
  claim survives without a citation. An empty retrieval returns a
  deterministic "no grounded answer" (the model is never called, so it
  cannot hallucinate), and an unconfigured/unreachable synthesis model
  fails closed to `-32603` rather than degrading to an ungrounded answer
  (reusing the #1386 `LlmClientUnavailable` Anthropic-Messages precedent).
  Single-shot Q→cited-A only; no new REST/CLI surface (the tool is
  auto-discovered) (#1526).
- `meho://retrieve/{query}` MCP resource (G0.5-T9) — exposes the G0.4
  hybrid-retrieval substrate through the MCP resource registry,
  percent-decoding the query path segment, sourcing `tenant_id` purely
  from the JWT, and returning the same `RetrievalHit` shape as
  `POST /api/v1/retrieve` (operator-min RBAC). Because the query rides in
  the URI path segment, the resource opts into a new generic
  `audit_redact_uri` flag on `ResourceTemplateDefinition`: when set, the
  dispatcher substitutes a query-stripped `meho://retrieve/<redacted>`
  sentinel for both the audit path and the payload URI, while
  correlatability is preserved via the existing `audit_query_hash` +
  `audit_hit_count` contextvars. The flag defaults off, so kb / memory /
  docs / tenant resources are unaffected (#348 / #1576).
- Add the `doc_collections` table (collections-as-data) — one row per
  documentation corpus, the docs analogue of the `targets` registry.
  Operator-set identity + backend binding (`collection_key` / `vendor` /
  `backend{type, ref}`) with probe-written liveness (`doc_count` /
  `last_ingested_at` / `readiness`, populated later). Global + tenant
  scoping via the dual partial-unique-index idiom, a tenant-first
  `resolve_doc_collection` lookup with a typed not-found, and a single
  `project_doc_collection_to_summary` ORM→wire projection. Foundational
  substrate for the catalogue and collection-scoped `search_docs`; no
  agent-facing surface yet (#1550).
- Add a `collection → backend{type, ref}` search router so one doc
  collection can sit on a managed RAG and another on the JWT-forward
  corpus behind the same `search_docs`, with the backend never appearing
  in the request or response. New `docs_search/backends/` package: a
  `SearchBackend` ABC (with a `probe()` seam for the later readiness
  Task), a tiny `dict[type, SearchBackend]` registry, and
  `resolve_backend` / `resolve_backend_or_label` (direct type lookup, no
  tie-break ladder; unknown/unconfigured type → the existing 503 arm).
  The single-corpus client is re-homed as the first `corpus-http`
  adapter, resolving its endpoint/audience per collection from
  `backend.ref` with the legacy `corpus_url` fallback for an unmigrated
  single-collection deploy. The `search_docs` service routes through the
  router via an additive, optional `collection` argument; threading a
  mandatory collection request param is a downstream Task (#1551).
- Make the doc-collection catalogue carry **backend readiness** so the
  router hides managed-RAG operational footguns. Fill in the
  `SearchBackend.probe(operator, *, backend_ref)` seam to return a typed
  `BackendReadiness` (reachable / index-built / doc count / last ingest);
  the `corpus-http` adapter reads it from the corpus `/status` endpoint
  and serializes concurrent rebuilds **per project** inside the adapter
  (an `asyncio.Lock` keyed on the corpus endpoint — no substrate
  scheduler). New tenant_admin-gated routes
  `POST /api/v1/doc_collections/{collection_key}/probe|enable|disable`: the probe
  **writes liveness back onto the row on success only** (a failed probe
  leaves it untouched, the `probe_target` split) and transitions the
  lifecycle `status` (`provisioning`/`rebuilding` → `ready` once the
  index is built); enable/disable are idempotent and guarded (forbidden
  transition → 409). A coarse `/ready` check reports each configured
  search backend reachable. New `meho docs collections probe|enable|disable`
  CLI verbs. The search-time "not-ready → typed 409/403" guard
  (`ensure_collection_searchable`) ships here; wiring it into the
  `search_docs` route is a downstream Task (#1552) (#1555).
- Make `collection` the mandatory binary scope on `search_docs` /
  `ask_docs` across all three surfaces (REST `POST /api/v1/search_docs`,
  the MCP tools + the `meho://docs/{collection}/{product}/{version}/{chunk_id}`
  resource, and `meho docs search --collection`). The query routes to the
  named collection's backend via the T2 router; `product` / `version`
  demote to optional metadata refinements within the collection (omitting
  them still succeeds). A missing/blank `collection` → 422 / `-32602`; an
  unknown collection → 422 / `-32602`. Add **per-collection entitlement**
  (reusing the `meho-docs` capability substrate, zero new tables): a
  principal may search a collection only when its tenant holds the
  `meho-docs:<collection>` capability — a miss → 403 / `-32602` even
  though the tool stays visible via the base `meho-docs` gate. A
  not-`ready` collection → 409 / `-32603`. Each call's audit row carries
  `audit_collection` alongside the canonical `meho.docs.search` /
  `meho.docs.ask` op_id (#1549), so rows are filterable by both op_id and
  collection; the raw query stays hashed. The shared resolve + entitle +
  readiness gate lives in `docs_search/collection_access.py` so all
  surfaces enforce one policy. CLI client regenerated for the new
  `collection` request field (#1552).
- Make the doc-collection catalogue **discoverable** so an agent learns
  which collections it may search before it searches. Add the
  `list_doc_collections` MCP tool (`required_capability="meho-docs"`,
  operator/read), the REST sibling `GET /api/v1/doc_collections`
  (operator), and the `meho docs collections list` CLI verb (`--vendor` /
  `--limit` / `--cursor` / `--json`, on the existing capability-gated
  `collections` parent). All three read `doc_collections` tenant-scoped
  (global + tenant rows, tenant row shadows a global key once), filter to
  the collections the principal is **entitled** to (`meho-docs:<key>` —
  the same per-collection key `search_docs` enforces, so every listed key
  is one `search_docs` accepts), keyset-paginate by `collection_key`, and
  bind the canonical `meho.docs.collections.list` audit op_id. Add an
  `initialize.instructions` catalogue band: the MCP `initialize` preamble
  now carries a guard-delimited `<<DOC_COLLECTIONS_AVAILABLE>>` block
  listing the operator's entitled collections (key / vendor / products /
  when-to-use + status), threaded via an optional `capabilities` keyword
  on `assemble_preamble`; the band is independently token-capped (an
  over-budget catalogue collapses to a summary pointing at
  `list_doc_collections`) and returns empty for a non-docs tenant, so an
  unprovisioned preamble is byte-identical to before. CLI client + OpenAPI
  snapshot regenerated for the new list route (#1553).
- Add opt-in **cross-collection fan-out** to `search_docs` (REST, MCP,
  and `meho docs search`): pass an explicit `collections=[a, b]` list
  (repeat `--collection` on the CLI) or the `collection="all"` sentinel
  (`--collection all`) to query several entitled collections at once. Each
  collection is searched independently on its own backend, and the per-
  collection ranked lists are merged by **reciprocal-rank fusion** (the
  house `RRF_K=60`) — never a raw-score sort, since scores are not
  comparable across backends/embedding models. Every returned chunk is
  tagged with its source `collection` for provenance. The fan-out resolves
  to **only entitled, ready** collections — non-entitled and not-ready
  members are dropped (logged, never silently truncated); an empty
  resolved set → 403 / `-32602`. A single `collection` and the fan-out
  scope are mutually exclusive (→ 422 / `-32602`). The audit row's
  `audit_collection` records the sorted, comma-joined queried set so
  who-touched attributes the fan-out. `ask_docs` stays single-collection
  only and rejects the fan-out shapes. CLI client regenerated for the new
  `collections` request field and the `DocsChunk.collection` provenance
  field (#1554).
- Add the server-side `secret.move` broker op (synthetic
  `secret-broker-1.x` identity, `requires_approval=True` +
  `safety_level="dangerous"`) and the `SecretEndpoint` adapter protocol
  with a kind-keyed registry, plus the first vault-kv↔vault-kv pair. A
  move copies one credential field between stores entirely inside the
  backplane; the value never enters the op params, response, logs, or the
  audit row — only the move status, the value SHA-256, and its length.
  The keycloak sink, approval-queue gating, CLI verb, and docs page are
  separate sibling tasks reusing this contract (#1577).
- Add a `keycloak`-kind `SecretEndpoint` sink so a `secret.move` can land
  a credential in a Keycloak user's password, proving the broker's
  cross-kind ("≥2 kinds") move surface (`vault:secret/...#password` →
  `keycloak:<target>/<realm>/<user>#password`). The sink reuses the
  existing Keycloak admin write path (username→UUID + `PUT
  .../reset-password`) and writes the value server-side from the
  in-memory `SecretMaterial`; the value never enters op params, the
  response, logs, or the audit row. Keycloak is write-only here, so the
  source side raises a clear unsupported error (#1578).
- Gate `secret.move` through the existing approval queue as a change-class
  op. A park-time preview builder records a **ref-only** `proposed_effect`
  on the approval request — the parsed `<kind>:<ref>` of `--from`/`--to`
  plus the operator reason, never the secret value (nor anything
  value-derived beyond hash/length). The time-boxed grant reuses the
  existing `AgentPermission` + approval `expires_at` machinery: an expired
  grant authorizes nothing and a parked request swept to `EXPIRED` is no
  longer decidable. Reuses the shipped approval substrate — no new
  capability/token system (#1579).
- Add the operator-facing `meho secret move --from <kind>:<ref> --to
  <kind>:<ref> --reason …` verb over the `secret-broker-1.x` connector
  (#1577). It is references-not-values: only the `<kind>:<ref>` source /
  sink references and the audit reason cross the wire — the secret value
  is never a flag, argument, or prompt, so it never lands in argv, shell
  history, or the op params. The change-class move requires approval, so
  the verb surfaces `status=awaiting_approval` verbatim (rendered, not
  treated as an error) and otherwise prints only the move status, the
  value SHA-256, and its byte length. Reuses the generic
  `/api/v1/operations/call` route, so the OpenAPI snapshot is unchanged
  (#1580).
- `meho.connector.ingest` MCP tool gains an `async=true` mode + a
  companion `meho.connector.ingest_status` poll tool (G3.5-T2),
  carrying the #1303 REST async-202 offload to the agent-facing MCP
  surface so a real vendor-spec ingest (e.g. SDDC Manager 9.0) returns
  a job handle immediately instead of blocking the parse+register+LLM-
  grouping pipeline past the agent's tool-call deadline. The async path
  reuses the existing in-memory `IngestJobRegistry` + `run_ingest_job`,
  so a job started over MCP is poll-able over the REST
  `GET /api/v1/connectors/ingest/jobs/{job_id}` endpoint and vice versa;
  the poll tool reports the run through to a terminal `succeeded`
  (final ingestion + grouping counts) or `failed` (`error_class` +
  `error`). `dry_run=true` and `async` unset keep the inline-return
  shape (no regression). The ingest tools moved into a new
  `connector_ingest` module alongside the existing `connector_admin`
  review/edit tools (#1531).

### Changed

- Connector ingest now rejects a **Swagger 2.0** spec with an
  *actionable* `UnsupportedSpecError` that names the conversion path —
  convert to OpenAPI 3.x (`swagger2openapi` / `converter.swagger.io`)
  and re-ingest the 3.x output — instead of a bare "not supported (
  v0.2.next)". The parser stays OpenAPI-3.x-only on purpose (no
  spec-conversion dependency pulled into the Python backend); the
  enriched diagnostic unblocks 2.0-only vendor surfaces such as Harbor
  2.x's `swagger.yaml` by telling the operator exactly what to do.
  OpenAPI 3.0.x / 3.1 ingestion is unchanged (#1532).
- **Security (SSRF):** the backend connector-spec fetch is now
  **https-only** — SSRF-guarded, streamed, 20 MiB-capped, and
  relative-redirect-safe — so a spec URL can no longer reach a local
  file (`file://`) or a non-https / internal scheme. To keep the
  `docs:` / `file://` on-ramps working without exposing the backend to
  local paths, the CLI now reads those spec sources **client-side** and
  uploads the bytes over a new `SpecSource.content` channel rather than
  handing the backend a path to fetch. Together these fold the #95 SSRF /
  local-file guard and the #102 content-upload on-ramp fix into one
  coherent change (supersedes #1477) (#95 + #102 / #1572).
- Remove the last references to the nonexistent `meho targets create`
  command outside the CLI surface that #1536 already fixed. The three
  backend `targets discover` docstrings (`CandidateHint`,
  `TargetsDiscoverResult`, `discover_targets`) — which propagate verbatim
  into `cli/api/openapi.json` and the generated Go client — now name
  `meho targets import`, and the regenerated snapshot/client follow. The
  five cross-repo onboarding docs (pfSense, BIND9, Holodeck, vmware-rest,
  Kubernetes) that showed an inline-flag `meho targets create --name …`
  block are rewritten to a `targets.yaml` descriptor + `meho targets
  import targets.yaml`, mirroring `argocd-onboarding.md` — a literal verb
  swap would have left an invalid file-based-import call. The dangling
  `(auto-registration is v0.2.next)` aside is reworded to
  `(one-shot auto-registration is not yet available)`, matching #1536. The
  verb now survives only in the two docs that state it does not exist
  (#1559).

### Fixed

- **Security (path traversal):** percent-encode operator-supplied `id` /
  `uuid` segments in the Keycloak Admin REST connector and add a UUID
  `pattern` gate. The fields were interpolated verbatim into f-string URL
  paths, and because httpx resolves `..` segments when merging a relative
  path against `base_url`, a traversal-shaped id (e.g.
  `../../../../realms/master/clients`) could escape the connector's
  configured `managed_realm` and reach any realm/admin path the broad
  admin service-account token can touch. A new `quote_segment()` helper
  (`urllib.parse.quote(..., safe="")`, mirroring the ArgoCD `_quote_name`
  precedent) is applied at all six path-interpolation sites, and a UUID
  `pattern` constraint on every `id`/`uuid` field in the op
  `parameter_schema`s makes the dispatcher's JSON-schema gate reject
  traversal-shaped input before any outbound call fires (#96 / #1476).
- **Security (secret disclosure):** clamp the `call_operation` envelope
  broadcast to the inner op's class. `handle_tools_call` computed the
  broadcast detail from the literal wrapper tool name `call_operation`,
  which `classify_op` maps to `other` → full detail, so the per-tenant
  broadcast event shipped `resolver_params` verbatim — including
  `params.data` for `vault.kv.put` and `params.password` for
  `vault.auth.userpass.write` — exposing secrets to every co-tenant feed
  subscriber. The handler now passes the inner `op_id` (from
  `arguments["op_id"]`) to `compute_effective_broadcast_detail`, so a
  credential-class inner op collapses to the aggregate-only
  `{op_class, result_status}` shape the inner DISPATCH row already uses;
  non-secret inner ops keep full-detail broadcast, and the envelope
  `op_id` / audit path stay the wrapper name so audit cardinality is
  unaffected (#93 / #1497).
- **Security (credential disclosure):** extend the `_API_KEY` redaction
  label set so free-text / error strings carrying `token` /
  `refresh_token` / `auth_token` / `session_token` / `secret_id` /
  `private_key` are masked. The pattern previously matched only `api_key`
  / `access_token` / `secret(_key)?` / `password` / `passwd` / `pwd` /
  `client_secret`, so a bare `token:` (and `secret_id:`, which
  `secret(?:[_-]?key)?` failed to match) slipped through — any connector
  whose upstream response embedded such a label in an error body passed
  the value to the agent and persisted it in the audit raw payload. Six
  more-specific members (`*_token` / `secret[_-]?id` / `private[_-]?key`)
  are added before the broad bare-`token` member so leftmost-first
  alternation never shadows them, and the module docstring + both policy
  reason strings are reconciled with the actual coverage so the gap
  cannot silently re-open (#94 / #1498).
- `meho.connector.ingest` over MCP now returns a typed `-32602 Invalid
  Params` with structured `error.data` for every spec-rejection class
  (`UnsupportedSpecError`, `InvalidSpecError`, `UpstreamNotSpecError`,
  `InvalidSchemaError`, `OpIdCollision`, `LlmOutputInvalid`) instead of a
  bare `-32603 "internal error: <ClassName>"` with the diagnostic
  discarded. Agents now get the same actionable detail the REST surface
  already carried (e.g. the Swagger-2.0 conversion path, "upstream
  served HTML, not a spec"), completing the #777 error-envelope pattern
  (#1534).
- NSX 9.x (VCF 9) is now ingestable into a dispatchable connector.
  NSX-T 4.x was renumbered onto the VCF train at VCF 9.0, but
  `NsxConnector` advertised `supported_version_range=">=4.0,<5.0"`, so a
  VCF-9 NSX appliance (which reports NSX 9.0.x and a 9.x `info.version`)
  could not be ingested under any label — the spec/label gate and the
  class version-range gate pincered every version. The range is widened
  to `">=4.0,<10.0"` and the class pin + catalog row track the
  VCF-9-aligned `9.0` line (the standalone NSX-T 4.x line still
  dispatches through the same class), and `apply_nsx_core_curation` gains
  a `connector_id` keyword so it curates the ops the ingest actually
  landed (e.g. `nsx-rest-9.1.0.0`) (#1530).
- The `docs:<connector-id>/<file>` spec-source shorthand is now honest:
  it is resolved **CLI-side only** (expanded to a `file://` URI against
  `$CLAUDE_RDC_DOCS`). Previously the schema docstring, CLI help, and a
  CLI comment claimed the backplane resolved `docs:` natively — it never
  did, so a bare `docs:` URI surfaced as an opaque
  `InvalidSpecError`/`-32603` that read like a missing file. The CLI now
  rejects an unset-`$CLAUDE_RDC_DOCS` `docs:` spec up front with a hint
  naming the env var, and the backend rejects any `docs:` URI that
  reaches the parser with a typed `UnsupportedSpecError` naming the
  scheme. `https://` / `file://` specs are unaffected (#1535).
- Reconcile the disabled-collection `search_docs` contract so code, the
  OpenAPI response descriptions, the `disable` endpoint docstring, and the
  CLI `disable` help all agree, and restore the operationally load-bearing
  **terminal/retryable** split the design intended (the shipped code had
  collapsed every non-`ready` status to a single 409, while two docstrings
  and the CLI help still claimed a 403). Searching a **`disabled`**
  collection now returns a **terminal** rejection — HTTP **403** with a
  structured `detail.error="collection_disabled"` (MCP **`-32602`** with
  `error.data.reason="collection_disabled"`) — distinct from a
  **transient** `provisioning`/`rebuilding` collection, which stays a
  **retryable** HTTP **409** (MCP **`-32603`**), so a client knows to back
  off and retry a rebuild but not a deliberately-disabled collection. The
  decision is made in **exactly one** readiness gate
  (`resolve_entitled_ready_collection`, shared by the REST route, the
  `search_docs`/`ask_docs` MCP tools, and the docs-chunk resource); the
  unwired duplicate guard (`ensure_collection_searchable` +
  `DocCollectionDisabledError`/`DocCollectionNotReadyError` in
  `docs_collections.lifecycle`) is removed. The `meho docs search` CLI
  renders the disabled 403 as a distinct "collection is disabled" error
  rather than the misleading `insufficient_role`. (Option A of the issue —
  honor the designed split — chosen over Option B's 409-for-all because the
  terminal/retryable signal is a genuine client value and the mechanism was
  already shipped and tested by #1555.) (#1567)
- Fix the optional `vendor` filter on `list_doc_collections` (MCP tool +
  `GET /api/v1/doc_collections` REST route) to run **after** the
  tenant-first dedupe instead of before it. Previously the filter was a
  pre-dedupe SQL `WHERE`, so when a tenant-curated row shadowed a global
  `collection_key` under a *different* vendor, filtering by the shadowed
  global row's vendor surfaced that global row's metadata
  (vendor/products/`when_to_use`/status) — violating the catalogue's
  documented "tenant row wins" invariant. The filter now applies in Python
  over the post-dedupe tenant-wins rows, so `vendor` only ever keeps or
  drops the row the principal would actually search; the keyset cursor
  (`collection_key > cursor`) stays in SQL and pagination is unaffected.
  Not an entitlement leak — the returned `collection_key` set and
  entitlement filtering are unchanged (#1568).
- Repoint `meho targets discover` (help text, post-run output, and the
  command doc-comment) from the nonexistent `meho targets create` to
  `meho targets import`, the verb that actually registers a reviewed
  candidate. The stale `(auto-registration is v0.2.next)` aside is
  reworded so it no longer dangles on a verb that does not exist (#1536).
- Stream `text/event-stream` responses through `AuditMiddleware` instead
  of buffering them. The middleware previously buffered the entire ASGI
  response before forwarding, so SSE endpoints (`/api/v1/feed`,
  `/ui/broadcast/stream`) delivered zero bytes until the stream closed —
  which for an open-ended feed is never, leaving the live-activity UI and
  `meho status --watch` dark on a real deploy (#1354 unmasked this by
  removing the spurious teardown that had been flushing the buffer with an
  error frame). The middleware now detects an SSE response via the
  `content-type` on `http.response.start` and forwards the start message
  plus every body chunk immediately; the audit row is still written at
  stream end (normal completion) or on a client-disconnect
  `CancelledError`, so the fail-closed "every authenticated action gets a
  row" contract holds for the streaming path. Non-streaming JSON routes
  keep the buffered fail-closed-500 contract verbatim (#1389 / #1585).
- Bind a canonical `audit_op_id` on the MCP `search_docs` / `ask_docs`
  handlers and make the dispatcher honor a handler-bound `audit_op_id` as
  an explicit override of the seeded tool-name op_id, so every docs query
  (REST / CLI / MCP) is filterable by `op_id=meho.docs.search` /
  `meho.docs.ask`. The same change lands the previously-ineffective dotted
  op_ids on the sibling MCP tools (`agent_runs`, `scheduler`, `approvals`,
  `agent_grants`, `agent_principals`), restoring the documented
  transport-independence of the audit op_id. `op_class` stays `read`; the
  raw query stays hashed (#1549 / #1558).

### Documentation

- Operator runbook for the `meho-docs` add-on:
  `docs/cross-repo/meho-docs-addon.md` (G4.5-T6). Covers what the add-on
  **is** (federated vendor-document layer, not ingested — vs the
  lightweight kb `search_knowledge`), **provisioning** (granting the
  `meho-docs` capability via the JWT `capabilities` claim from T1, plus
  the `CORPUS_*` settings from T2 the deploy needs), **verify** (the
  surface present + returning cited chunks on a provisioned tenant,
  absent on an unprovisioned one, the per-face audit row visible via
  `meho audit query` — `meho.docs.search` for the REST route + CLI verb,
  `search_docs` for the MCP tool, the dispatcher's tool-name-verbatim
  convention), and the one-line **routing convention** —
  "ask the team first (`search_knowledge` / `search_memory`), escalate
  to `search_docs` only on a miss or an explicit vendor-fact need" —
  matching the shipped T4 tool description. Notes the external
  MEHO.Knowledge → meho-docs corpus rename is ops-side, tracked on the
  consumer repo (#1525).
- Update the operator provisioning runbook
  (`docs/cross-repo/meho-docs-addon.md`) to the **doc-collection
  catalogue** model now that the whole G4.6 feature has shipped. The page
  documents the collection model (a named corpus bound to a backend; the
  agent picks a `collection`, never a backend) and the `doc_collections`
  row shape; **per-collection provisioning** (the `meho-docs` add-on key
  gates the surface, `meho-docs:<collection_key>` entitles a tenant to a
  specific collection — reusing the same JWT capability claim, zero new
  substrate); seeding a collection with its `backend{type, ref}` routing
  record (`corpus-http` adapter + the legacy `corpus_url` fallback) and
  bringing it to readiness through the **probe → enable** lifecycle
  (`provisioning`/`rebuilding` → `ready`; `disable` → not-ready, so
  search fails typed — a terminal 403 for `disabled`, a retryable 409
  for `provisioning`/`rebuilding`, see the contract reconciliation in
  #1567);
  **discovery** (`list_doc_collections` / `GET /api/v1/doc_collections` /
  `meho docs collections list` + the `initialize.instructions`
  `<<DOC_COLLECTIONS_AVAILABLE>>` band); the **search contract**
  (`search_docs --collection` mandatory binary scope, `product`/`version`
  optional refinements, cross-collection fan-out via repeated
  `--collection` or `--collection all` with RRF merge, `ask_docs`
  single-collection only); and the **backend-agnostic** note
  (`collection → backend{type, ref}` resolved server-side). Each
  command / flag / status-code claim (422/403/409/503 ↔ `-32602`/`-32603`,
  CLI exit 4/5) matches the shipped surface. States the routing convention
  verbatim: *ask the team first — `search_knowledge` / `search_memory` —
  escalate to `search_docs(collection=…)` only on a miss or an explicit
  vendor-fact need; pick the collection explicitly (it's a binary filter,
  not a guess).* Docs-only — no code or API-surface change (#1556).
- Document the hand-authored-OpenAPI-3.x → `--spec file://…` route as the
  intended on-ramp for products that publish **no** OpenAPI spec (VCF
  Fleet / vRSLCM, Hetzner Robot): a "Product publishes no OpenAPI spec"
  section in `docs/cross-repo/connector-ingestion.md` with a minimal
  worked example, and the catalog-miss `next_step` rationale widened to
  name it so a 0-op `state=registered` connector no longer reads as a
  dead end (#1533).
- Add the operator/reviewer-facing runbook
  [`docs/cross-repo/secret-broker.md`](docs/cross-repo/secret-broker.md)
  for the G0.22 secret broker — the `<kind>:<ref>` move-intent schema
  (shipped `vault` source+sink and `keycloak` sink kinds), the value-free
  `{status, value_sha256, length}` response, the "agent never observes the
  value" guarantee tied to each enforcing mechanism (no value-bearing
  flag / param / response / log / audit row / approval `proposed_effect` /
  broadcast), and the enlarged threat model — the backplane as a
  credential-bearing intermediary, mitigated by operator-context reads,
  the `dangerous` deny-by-default / needs-approval-ceiling lattice,
  mandatory four-eyes approval, time-boxed `AgentPermission` + approval
  `expires_at`, `params_hash` tamper-evidence, and hash-only audit. Names
  the deferred token-minting / diff-shaped-approval follow-ups so neither
  is mistaken for shipped, and links from the `docs/cross-repo/` runbook
  index. Documents the actual merged behaviour, including where the CLI
  (`--reason` required) tightens the op schema (`reason` optional) and
  that a parked move exits 0 (#1581).

## [0.11.0] - 2026-06-05

### Added

- Add a read-back surface for materialized JSONFlux result handles over
  MCP: a large (`>50`-row / `>4 KB`) reducing dispatch now spills its
  **full** row set to a Valkey-backed `ResultHandleStore`
  (tenant+handle-scoped key, the handle's `ttl_seconds` as a
  server-enforced expiry, row count capped by
  `RESULT_HANDLE_MAX_SPILL_ROWS`, default 10000) instead of discarding
  every row past the inline sample at reduce time. The new `result_query`
  MCP meta-tool pages the full set back (`handle_id` + `offset`/`limit`,
  operator+tenant scoped — a cross-operator or cross-tenant read is an
  indistinguishable not-found miss), and the handle's
  `fetch_more.drill_in` now flips to `available=true` naming the tool, an
  `example_call`, and the handle's `expires_at` (no longer hardcoded
  `false`). The spill is fail-open: an unreachable store leaves the inline
  sample shipping exactly as before (#1507).

### Fixed

- Wire the agent-run lease/heartbeat into the fire path so a hung, crashed, or worker-killed run is reliably reaped to a terminal `failed` state instead of staying `running` forever; the run loop now stamps a lease on start and heartbeats while alive, and child (`invoke_agent`) runs are leased too (#1501).
- Bound the scheduler tick's wait on a scheduled agent run so a hung or
  approval-gated run can no longer block later triggers — or strand the
  process-wide advisory lock — until a pod restart. `run_scheduled` now
  waits on the run via `asyncio.wait_for(asyncio.shield(task), …)` capped
  at `AGENT_SYNC_TIMEOUT_SECONDS` (default 30s, mirroring the human
  `run()` path); a run still executing at the deadline keeps running in
  the background (`converted_to_async`) while the serial tick returns and
  releases its advisory lock each cadence
  ([#1502](https://github.com/evoila/meho/issues/1502)).
- Execute a parked direct operator op when it is approved via `/decide`
  or the MCP/CLI by-id approve, not only via REST `/approve`: the
  approval decision now drives the re-dispatch using the params stored on
  the request at park time, so an approved direct write lands its effect
  exactly once. Agent-run resume is `run_id`-gated and unchanged (#1503).
- Return a clean structured `target_required` error when a
  target-requiring typed/composite op (e.g. `keycloak.user.list`) is
  dispatched with no `target`, instead of the opaque
  `connector_error: RuntimeError` ("…reached dispatch still unbound…
  instance-cache fault…") it previously surfaced. The dispatcher now
  catches the no-target case at connector-resolution time, keyed on
  handler shape (a connector-bound, self-first handler needs a target; a
  module-level handler does not), so a legitimately target-less op still
  dispatches and the loud self-guard `RuntimeError` stays in place for
  genuine instance-cache faults (#1506).
- Flag a Vault KV write (`vault.kv.put`/`patch`/`delete`) the dispatching
  identity lacks capability for **at park time** instead of after a
  four-eyes approval: `_handle_needs_approval` now probes
  `POST sys/capabilities-self` on the target `<mount>/data/<path>` and
  surfaces a `permission_preflight` banner (`will_be_denied: true` when
  the token lacks `create`/`update`) on the approval row, so an operator
  is not asked to approve a write Vault will then deny. The probe returns
  only capability names — never a secret value — so it sidesteps the
  credential-class redaction rule. Also documents the `meho-mcp` role's
  required KV write-capability policy stanza + a `sys/capabilities-self`
  verify command in `docs/cross-repo/connector-vault-policy.md` (#1504).
- Fail a no-inputs scheduled run with a typed `scheduled_run_no_input`
  classification instead of an opaque provider 400. A cron/one-off/event
  trigger created without `inputs` is still accepted at create (whether a
  user turn is needed depends on the referenced agent definition), but at
  fire time the scheduled-run seam now detects the empty user prompt
  *before* the model call and finalises the run `failed` with a greppable
  `scheduled_run_no_input` error — rather than letting it reach the
  provider as a system-prompt-only request with an empty `messages` array
  (which every supported backend rejects with "messages: at least one
  message is required"). The scheduler logs `scheduler_fired_run_failed`
  so the misconfiguration is visible at fire time; no synthetic user turn
  is injected (#1505).

### Documentation

- Document that the scheduler's Vault agent-credentials path uses the **sanitised, UPPER-CASED** `client_id`, not the raw `identity_ref`: `vault_path_for_client_id` substitutes the sanitised + `upper()`-cased form into `SCHEDULER_AGENT_VAULT_PATH_PATTERN`, so `agent:ops-writer` resolves to `secret/data/agents/AGENT_OPS_WRITER/credentials`. Write and read share the one helper and cannot diverge; `docs/codebase/scheduler.md` and the `settings.py` field comment now carry a worked example so an operator hand-provisioning the Vault secret/policy targets the right path (#1508).

## [0.10.1] - 2026-06-04

### Fixed

- Connector credential handling: every Vault-sourced credential is now
  whitespace-stripped before use (a `client_secret` stored with a
  trailing newline was sent verbatim and rejected by Keycloak as
  `unauthorized_client`, surfacing only as an opaque `HTTP 401`). A shared
  `strip_credential_value()` in `_shared/vault_creds.py` is applied at
  every credential-field extraction path (`load_basic_credentials`
  consumers — vmware, nsx, harbor, sddc, argocd, vcf — plus the Keycloak
  admin + GitHub App/PAT loaders), and `KeycloakAdminTokenError` now
  surfaces the OAuth2 `error`/`error_description` instead of only the HTTP
  status ([#1475](https://github.com/evoila/meho/issues/1475)).
- **Security (credential disclosure): a failed scheduled agent run no
  longer writes the agent's `client_credentials` secret into the JSON
  logs.** On the `scheduler_fire_failed` path the secret was held as a
  plain-`str` frame local, and structlog's traceback renderer
  (`dict_tracebacks`) defaults to `show_locals=True`, so every failed
  fire serialised the secret to stdout in cleartext (CWE-532). Two
  defenses now apply: the secret is threaded as a `pydantic.SecretStr`
  from `_PreparedInvocation` through `run_scheduled` (masking to
  `'**********'` even as a bare frame local, unwrapped only at the
  token-mint call site — the first `SecretStr` in the backplane), and
  `configure_logging` runs the traceback transformer with
  `show_locals=False`, dropping every frame's locals dict (which also
  closes the latent `auth/agent_token.py` frame where the secret is an
  unavoidable plain `str` for the httpx form-post). The structured
  traceback (file / line / function / exception type) is retained for
  triage (#1488).
- **`agents.delete` on a definition that ever had a `scheduled_trigger`
  (including a cancelled one) no longer fails with an opaque
  `-32603 "internal error: IntegrityError"` (MCP) / unhandled HTTP 500
  (REST).** The `scheduled_trigger.agent_definition_id` FK was created
  without an `ondelete` clause (default `NO ACTION`), so deleting a
  once-scheduled definition violated the constraint — and because
  `cancel()` retains the trigger row for audit and there is no API path
  to hard-delete it, such a definition was permanently undeletable, only
  `enabled=false`-able. Migration `0035` adds `ON DELETE CASCADE` to the
  FK (a DB-level cascade, since the delete is a bulk Core statement that
  bypasses ORM relationship cascades), so deleting a definition
  cascade-deletes its dependent trigger rows on both MCP and REST.
  `agent_run` history is a nullable soft-FK and is unaffected. (#1480)
- The `self_approval_forbidden` REST/MCP error strings now carry the
  `APPROVAL_ALLOW_SELF_APPROVAL` break-glass hint that the underlying
  `SelfApprovalForbiddenError` already constructs, surfaced on all three
  operator-facing catch sites (REST `/approve` + `/decide`, MCP
  `meho.approvals.approve`); `self_approval_forbidden` is preserved as a
  stable token prefix (#1483).
- Scheduler now sources an agent's `client_credentials` secret from Vault
  instead of a pod environment variable, so an agent registered + defined
  purely over the API is schedulable with no `MEHO_AGENT_SECRET_*` env var
  and no redeploy. Registration captures the Keycloak-generated client
  secret and persists it to Vault under a scheduler service token
  (`VAULT_SCHEDULER_TOKEN`); `resolve_agent_credentials` reads it
  Vault-first, keeping the env var as a documented break-glass fallback
  (#1478).
- A scheduled run for an agent registered purely over the API no longer
  dies fail-closed at JWT verify (pre-dispatch) with `missing_audience` /
  `missing_sub` / `missing_tenant_claim`. `agent_principals.register` now
  provisions the agent's Keycloak client with the **same** mapper + scope
  set the working `meho-backplane` client carries: an `oidc-audience-mapper`
  stamping `aud=KEYCLOAK_AUDIENCE` (stock Keycloak ignores the RFC 8707
  `audience` request param on a `client_credentials` grant without a
  configured mapper), the default client scopes (`basic`/`roles`/
  `web-origins`/`acr`) that carry `sub` (Admin-REST-created clients do not
  inherit them), and the `tenant_id`/`tenant_role`/`principal_kind=agent`
  hardcoded-claim mappers. An API-registered agent now authenticates
  end-to-end and reaches an operation / parked approval with no manual
  Keycloak surgery (#1487).
- **Approval-queue audit fidelity (G0.19-T4).** A self-approval (and any
  other post-gate `McpInvalidParamsError` — `approval_request_not_found`,
  `approval_unauthorized`) rejection over MCP now audits with a `403`
  "denied" status consistent with the JSON-RPC `-32602` wire outcome,
  instead of a misleading `500`; the live broadcast event is classified
  "denied", not "error". Delegated agent runs now record
  `principal_act=agent:<name>` on the parked `ApprovalRequest` row (read
  from the same `actor_sub` delegation context the audit log uses);
  previously this field read a nonexistent `Operator.identity_act` and
  was always null. Direct human approvals keep `principal_act=NULL`
  (#1481).
- JSONFlux: a large list response reduced to a `ResultHandle` (e.g.
  `k8s.logs`) now previews the **most-recent** rows inline instead of
  the oldest. Connectors whose op returns a chronologically-ordered
  collection declare `llm_instructions.result_ordering = {"sample":
  "tail"}`; the reducer samples the tail of the set (the bottom of a
  `kubectl logs` window) rather than a bare `LIMIT`. Connector
  agent-facing strings that pointed at a `result_query` /
  `result_describe` / `HandleStore` read-back surface that does not
  exist were corrected to the truthful guidance (re-call with narrower
  params / native pagination); string-shaped outputs such as `k8s.exec`
  are unaffected (#1479).
- `list_operation_groups` / `search_operations` now return a typed
  `connector_not_ingested` hint for a connector that is v2-registered but
  not yet ingested (0 DB rows, `state="registered"`) instead of an opaque
  `-32603 UnknownConnectorError` over MCP. The error carries the same
  `meho connector ingest …` next-step verb the `GET /api/v1/connectors`
  listing already emits (`-32602` + `error.data.reason` over MCP; `404`
  with a structured `detail` over REST), and stays distinguishable from a
  genuinely unknown connector_id so an agent can self-correct
  ([#1482](https://github.com/evoila/meho/issues/1482)).

## [0.10.0] - 2026-06-01

The **connector write-surface** release: MEHO connectors graduate from
read-only to **mutating operations gated behind a human approval
queue**, two new connectors (ArgoCD, Keycloak) land at read +
approval-gated write, write surfaces are added to the kubernetes /
vault / VMware connectors, and the Runbooks operator console ships at
`/ui/runbooks`.

### Added

- **Human approval queue for connector writes (G11.7).** Every mutating
  connector operation is now parked for explicit human approval before
  dispatch: a queue with a self-approval guard (the operator who
  proposes a write cannot approve their own), write-op request/response
  redaction, and a resume-target fix so an approved write resumes
  against the intended call (#1422). A **dual-run soak harness** gates
  write-op graduation through a five-stage check before an op is allowed
  to dispatch for real (#1423).

- **ArgoCD connector — L1-typed GitOps control (G3.12).** A new
  `ArgoCdConnector` (`HttpConnector` subclass) authenticating with a
  **bearer token loaded from Vault** and fingerprinted via
  `GET /api/version`: skeleton + credential loader + dual registration
  (#1440), a curated read core (`app.list/get/diff/resource_tree`) via
  `register_typed_operation` (#1442), CLI/MCP verbs + recorded-fixture
  E2E + onboarding doc (#1444), and **approval-gated write ops**
  (`app.sync/rollback/set`) with CLI write verbs (#1446) wired to a
  park-time `proposed_effect` preview (#1457).

- **Keycloak connector — Admin-REST realm control (G3.13).** A new
  `KeycloakConnector` authenticating with a **Keycloak admin
  `client_credentials`** token, deliberately distinct from the
  operator-OIDC path to avoid a bootstrap circular-auth dependency:
  skeleton + admin credential loader (#1439), secret-redacted curated
  read ops (#1441), CLI verbs + dispatch token-refresh E2E + onboarding
  doc (#1443), and approval-gated write ops (realm / client / scope /
  protocol-mapper) with CLI verbs (#1445).

- **Approval-gated write/mutating ops on the kubernetes, vault, and
  VMware connectors (G3.14 / G3.15 / G3.16).**
  - **kubernetes:** single-call write ops (#1425) and `k8s.exec` —
    bounded command-and-capture over a `WsApiClient` websocket
    transport (#1424).
  - **vault** (token auth): kv writes (`put` / `delete`) plus new
    `kv.patch` (#1426); policy read/list (safe) + write/delete
    (approval-gated) (#1428); auth credential lifecycle write ops with
    request/response secret redaction (#1427); identity + token ops —
    entity/group writes + token `create` / `revoke_accessor` /
    `list_accessors` (#1430); sys bootstrap writes — auth/mount
    enable + tune (#1429).
  - **VMware (VCF) write activation:** reconcile the 8 vmware
    write-composite L2 `op_id`s with ingest (#1431), verify the
    composites preflight + dispatch behind the approval queue (#1432),
    and wire `host.detach_from_vds` onto the dual-run soak harness
    (#1433).

- **Runbooks operator console at `/ui/runbooks` (G10.6).** A
  server-rendered HTMX surface over the G12 runbook-templates API:
  catalog browse + opacity-floor-aware detail (#1396), a tenant_admin
  authoring editor (draft + edit) with a CodeMirror discriminated-union
  step form (#1419), publish / deprecate / fork-on-edit lifecycle
  controls (#1420), and surface docs + discoverability + an end-to-end
  acceptance test (#1421).

- **Production ingest LLM client wired at lifespan startup (G3.17,
  #1418).** The grouping LLM client is now constructed at backplane
  startup so `--catalog` ingest groups + enables L2 connector
  operations on a deployed backplane (degrades gracefully when no key
  is set) — the keystone that makes the typed/generic connector
  surfaces above dispatchable on a real deploy.

- **`proposed_effect` park-time previews (#1454).** A builder hook
  auto-populates a `k8s.apply` dry-run preview at park time so an
  approver sees the predicted effect before granting a write.

### Changed

- README reworked into a credible front door: restructure + residual
  T1 fixes (#1456), positioning + relocated values tables and cosign
  recipes (#1458), and corrected stale factual claims for v0.9.0
  (#1453).
- A README version-drift guard workflow was added (#1455) and made
  tolerant of a badge-only version surface (#1460).
- Migrated testcontainers `wait_for_logs(str)` →
  `LogMessageWaitStrategy` (#1461).
- Roadmap: slot v0.10 as the connector write-surface release (#1417).
- G3.17-T2 operator runbook documenting the `ANTHROPIC_API_KEY`
  dependency for ingest on a deployed backplane (#1438).

### Fixed

- Reject `null` in the `vault.kv.patch` data schema at every depth
  (JSON-merge correctness) (#1462).
- Strengthen the composite preflight test to assert dispatch did not
  generically error (#1463).

## [0.9.0] - 2026-05-31

### Added

- The `?envelope=v2` list-envelope opt-in now works on all five §2 list
  endpoints: `GET /api/v1/connectors`, `GET /api/v1/conventions`,
  `GET /api/v1/audit/my-recent`, and `GET /api/v1/broadcast/overrides`
  join `targets` and the topology `dependents`/`dependencies` endpoints
  in returning the unified `{items, next_cursor?, …sidecars}` shape when
  the param is passed; omitting it keeps the v0.8.0 default shape so no
  client breaks. Completes #1312 acceptance A (the deferred
  "A-remainder"). (#1356 — RDC #789 Finding 3,
  `list-endpoint-envelope-asymmetry`)

- **Helm chart first-class wiring for agent-runtime credentials
  (G0.18-T10 #1363).** Two new top-level chart blocks land so an
  operator enables the G11.1 agent LLM loop and G11.2 agent-principal
  registration without hand-rolling Kubernetes Secrets + `extraEnv`
  `valueFrom` plumbing: `agent.enabled` wires `ANTHROPIC_API_KEY` and
  `keycloakAdmin.enabled` wires the three `KEYCLOAK_ADMIN_*` envs into
  the backplane Deployment. The two confidential credentials
  (`ANTHROPIC_API_KEY`, `KEYCLOAK_ADMIN_CLIENT_SECRET`) are always
  rendered as `secretKeyRef` — never plaintext chart values or env
  values — mirroring the existing `postgres.credentialsSecret` and
  `eso.keycloak` precedents; `KEYCLOAK_ADMIN_URL` and
  `KEYCLOAK_ADMIN_CLIENT_ID` are plain operator config and render as
  `value:`. Both blocks default `enabled: false`, so a deploy that
  doesn't want either feature stays fail-closed (`/api/v1/agent-runs`
  → "no credentials"; `POST /api/v1/agent-principals` →
  `503 keycloak_admin_not_configured`) — no behaviour change for
  existing operators. Two new opt-in ExternalSecret rendering paths
  (`eso.agent.enabled`, `eso.keycloakAdmin.enabled`) materialise
  `<release>-agent` / `<release>-keycloak-admin` Secrets from Vault
  in parallel to the existing `eso.keycloak` story; the Secret-name
  resolution helpers (`meho.agentSecretName`,
  `meho.keycloakAdminSecretName`) let operators pick BYO Secret or
  ESO-rendered Secret without reconciling names. A new
  `helm test`-triggered Pod
  (`templates/tests/test-agent-runtime-config.yaml`) and a chart-CI
  grep gate (in `.github/workflows/chart.yml`) assert the wired-up
  shape so a regression that flips either secret to plaintext is
  rejected at PR-build time. Closes the chart-side gap that prevented
  operators from enabling agents on a Helm deploy without a manual
  `extraEnv` workaround.

<!-- bulk roll-up (per-PR bullets authored at release time) -->
- G0.12-T2 operation verbs use generated typed client (#1275)
- G12.1-T1 migration 0034 + SQLAlchemy models + audit_log run_id/step_id columns (#1327)
- G12.1-T2 run_id_var + step_id_var contextvar plumbing for runbook correlation (#1328)
- G12.2-T1 runbook template Pydantic schemas + step-shape discriminated-union validation (#1331)
- G12.2-T2 runbook template service layer — CRUD + fork-from-published + in_flight_run_count (#1333)
- G12.2-T4 runbook template MCP tools — runbook_*_template × 6 (#1335)
- G12.2-T3 runbook template REST routes under `/api/v1/runbooks/templates` (#1336)
- G12.3-T1 run-side Pydantic schemas — opacity-shaped single-step response (#1338)
- G12.3-T2 step-execution engine + runtime substitution helper (#1339)
- G12.3-T3 run service layer — start/next/abort/reassign/list + post-completion check (#1340)
- G12.3-T4 post-completion show_template carve-out (#1341)
- G12.3-T6 runbook run MCP tools — start/next/abort/reassign/list × 5 (#1343)
- G12.3-T5 runbook run REST routes under `/api/v1/runbooks/runs` (#1342)
- G12.4-T1 runbook priming helper (#1346)
- G12.4-T2 wire runbook priming into MCP initialize preamble (#1347)
- G12.5-T1 meho runbook CLI chassis + 6 template verbs (#1349)
- G12.5-T2 meho runbook CLI run verbs — start/next/abort/reassign/runs (#1350)
- G0.18-T10 helm chart first-class agent-runtime secret wiring (#1373)

### Changed

- G0.12-T1 migrate to generated typed client (#1276)
- G0.12-T3 migrate cmd/agent/ to the generated typed client (#1277)
- G0.12-T4 migrate cmd/agent-principal/ to typed client (#1262 #1279)
- G0.12-T6 migrate to generated typed client (#1264 #1280)
- G0.12-T7 migrate cmd/connector/ to typed client (#1265 #1283)
- G0.12-T8 migrate cmd/conventions/ to typed client (#1266 #1284)
- G0.12-T9 migrate cmd/kb/ to typed client (#1267 #1282)
- G0.12-T10 migrate cmd/memory/ to typed client (#1268 #1287)
- G0.12-T11 migrate cmd/migrate/ to typed client (#1269 #1285)
- G0.12-T12 migrate cmd/retrieval/ to typed client (#1270 #1286)
- G0.12-T13 migrate cmd/scheduler/ to typed client (#1271 #1291)
- G0.12-T14 migrate list/describe/probe/discover to the generated typed client (#1272 #1289)
- G0.12-T15 migrate cmd/topology/ to typed client (#1273 #1290)
- G0.12-T16 promote dispatch.Connector to own typed transport (#1274 #1293)
- G0.12-T5 migrate to generated typed client (#1263 #1281)
- refresh shipped status — v0.6/v0.7/v0.8 → shipped (#1288)
- add api-shape-conventions.md — SEV-4 sweep + curated-daily-driver framing (#1310)
- §10 intra-connector list-op request-shape parity (#1334)
- G12.2-T5 multi-session drafting authoring guide at docs/runbooks/authoring.md (#1337)
- record v0.8.1 release on main (#1344)
- G12.3-T7 runbook architecture doc at docs/architecture/runbooks.md (#1345)
- G12.4-T3 document runbook session priming in mcp.md (#1348)
- G12.5-T3 meho runbook operator CLI reference (#1351)
- unblock v0.9.0 release tooling + reconcile roadmap (#1379)

### Fixed

- Agent runtime no longer 404s on the shipped default model id: the
  `provider:` prefix of a pydantic-ai spec-form id
  (`anthropic:claude-sonnet-4-6`) is now stripped before constructing
  `AnthropicModel`, at both the G11.5 backend resolver and the
  pre-resolver default path. A prefixed override (the documented spec
  form) and a deploy-supplied bare id both still work. (#1375 — RDC #789
  N11)

- **Manually-seeded topology nodes are now visible to
  `query_topology kind=history` / `kind=timeline` (G0.18-T6 #1359,
  RDC #789 F-A).** `meho.topology.create_node` wrote `audit_log` +
  one broadcast event but no `graph_node_history` row, so a manual
  seed was invisible to the per-resource history walk and the
  tenant-wide timeline even though it surfaced in `query_audit` —
  an audit-vs-graph-history asymmetry surfaced by the RDC consumer
  finding when operators bootstrapping non-k8s targets via
  `create_node` could not answer "when was this node added?"
  through the history/timeline verbs. The hook now emits one
  `graph_node_history` row per meaningful call sharing the call's
  pre-allocated `audit_id` (chassis pre-allocation pattern shared
  with refresh / annotate so history rows join back against
  audit_log to recover the causing principal). Idempotent re-seeds
  whose only change is the heartbeat `seeded_at` / `last_seen`
  fields deliberately skip the emit — mirrors
  `refresh._update_existing_node`'s `is_meaningful_update`
  discipline and `annotate._annotate_curated_is_meaningful`'s
  heartbeat strip — so a polling MCP agent does not balloon the
  history table with empty UPDATED rows.

- **`POST /api/v1/targets` accepts the `meho connector list` SDDC
  product token (G0.18-T2 #1355).** Closes #1312 acceptance B, which
  had been marked "already aligned" but the split persisted:
  `meho connector list` emits `product="sddc"` (parser-derived from
  `sddc-rest-9.0`, load-bearing for the #773 connector_id
  round-trip), while the v2 registry, the spec catalog, and the
  `TargetCreate` validator all use the canonical `sddc-manager`.
  An operator copying the listing token into a create now succeeds:
  a `PRODUCT_ALIASES` map in
  `meho_backplane.connectors.registry` normalises `sddc` →
  `sddc-manager` at the write surface (`POST` + `PATCH
  /api/v1/targets`) before the registered-product validator runs,
  and the canonical token is what gets stored — so the resolver,
  audit log, and every list / detail read see one spelling
  regardless of which the operator typed. A new structural test in
  `test_operations_ingest_catalog.py` pins the round-trip for
  every shipped connector so a future drift fails CI rather than
  surfacing on the next dogfood cycle. RDC #789 Finding 6.

- **Fresh SSE broadcast-feed connections no longer die at ~5 s with a
  spurious `feed_error` frame (G0.18-T1 #1354, RDC #789 N1).** The
  single process-wide broadcast client pinned `socket_timeout=5.0`
  for the fail-fast readiness probe, but redis-py 7.4 resolves
  `xread`'s read-timeout from `socket_timeout` when no per-call
  override is supplied — so every `XREAD BLOCK 30000` against a
  quiet stream raised `redis.TimeoutError` at ~5 s and the SSE
  generator yielded a `broadcast_subsystem_unavailable` frame. The
  fix splits the substrate into two cached clients: `get_broadcast_client()`
  (`socket_timeout=5 s`, for the readiness `PING` / publish hot path
  / SSE backlog prelude) and `get_broadcast_blocking_client()`
  (`socket_timeout=35 s` = 30 s BLOCK + 5 s buffer, for every
  blocking-XREAD caller — SSE feed, UI SSE bridge,
  `meho.broadcast.watch` MCP tool, agent approval-wait loop). A
  quiet BLOCK now returns `None` (the natural keepalive) and the
  generator emits a heartbeat; only genuine transport failures past
  the 35 s window still raise the T11 error frame. The readiness
  probe's 5 s SLO is explicitly preserved.

- **Ingest LLM-grouping docs + `composite_l2_missing` envelope —
  honest "build-time-only" framing, dead `#405` reference removed
  (G0.18-T7 #1360, RDC #789 N9).** The previous wording cited
  `T5 (#405)` / "production Anthropic adapter lands with G0.7-T5"
  in multiple docstrings (`operations/ingest/pipeline.py`,
  `api/v1/connectors_ingest.py`, `mcp/tools/connector_admin.py`,
  `docs/codebase/spec-ingestion.md`, two test files), but `#405`
  was G0.7-T5 = CLI verbs (CLOSED) and never tracked an LLM
  adapter — and `settings.anthropic_api_key` flows only to the
  agent runtime, so non-dry-run `meho connector ingest --catalog
  <product>/<version>` 503s on every deploy (the chassis
  `LlmClient` factory is fail-closed by default and FastAPI
  lifespan startup has no caller for `set_llm_client_factory`).
  The `composite_l2_missing` error envelope's escape-hatch hint
  now names the limitation explicitly so operators don't follow
  the suggested catalog command into a silent 503. New
  `docs/codebase/spec-ingestion.md` §"LLM-client wiring (build-
  time-only today)" documents the gap. Wiring a production
  `LlmClient` adapter at lifespan startup remains the
  operator-side follow-up.

- **VCF-family catalog rows + `GET /api/v1/connectors` `next_step`
  hints no longer over-promise `--catalog` ingest (G0.18-T8 #1361,
  RDC #789 N8).** Rechecked the upstreams against G0.15-T2 (#1211):
  `vmware/9.0` and `sddc-manager/9.0` still serve `text/html` from
  the Broadcom Developer Portal (no regression — the catalog notes
  already document the unusability, the route's
  `catalog_entry_upstream_not_spec` 422 still fires). `nsx/4.2`
  is still fqdn-templated (`<nsx-mgr-fqdn>`) under
  `catalog_entry_templated_upstream`. The over-promising was
  isolated to the listing's hint: for any `state="registered"`
  row whose catalog entry exists, the hint blindly said "spec
  available in catalog; run ingest" and pointed at
  `--catalog <product>/<version>` — which 422'd for all three
  VCF-family rows. Added a declarative `catalog_ingest:
  "supported" | "spec-only"` field on `ConnectorSpecEntry`
  (default `"supported"` for back-compat; the three VCF rows
  opt into `"spec-only"`); the listing's `next_step` hint now
  branches on it and emits the explicit-quadruple `--product …
  --version … --impl … --spec <concrete-openapi-uri>` verb plus
  a rationale calling out the upstream-shape limitation when
  the row is spec-only. Route validation behaviour is unchanged
  (the existing 422 envelopes still fire on direct catalog-shape
  POSTs against these rows); the hint is now an honest
  precursor instead of pointing operators at a broken verb.
  Docs: [`connector-catalog.md`](docs/cross-repo/connector-catalog.md)
  §"Spec-only entries" + entry-schema table.

- **Topology blast-radius distinguishes untracked from
  no-dependents; `annotate` §6 over-warning softened (G0.18-T4
  #1357, RDC #789 N2 + N7).** Pre-fix, `query_topology
  {kind: dependents}` returned `[]` for both "the anchor isn't in
  the graph at all" and "the anchor is tracked but nothing depends
  on it." Auto-discovery is k8s-only — only
  `KubernetesConnector` overrides `Connector.discover_topology`;
  every other shipped connector inherits the no-op ABC default —
  so every registered `vault` / `vcenter` / `nsx` /
  `sddc-manager` / `gh` target started life untracked, and the
  pre-destructive blast-radius use case read the `[]` as "safe to
  delete." `find_dependents` / `find_dependencies` now resolve the
  anchor via `resolvers.resolve_node` up front and raise
  `NodeNotFoundError` on a miss; the REST front maps that to
  **404 `node_untracked`** (distinct slug from the annotate
  flow's `node_not_found` because the operator action diverges —
  closure: register / refresh the target or annotate the
  relationship; annotate: seed the endpoint via
  `meho.topology.create_node`), the MCP front returns the typed
  `{kind, status: "node_untracked", name, nodes: []}` envelope,
  and the CLI renders an operator-actionable line. A
  tracked-but-no-dependents anchor still returns the one-element
  `[root]`. Separately, the `annotate` tool description's blanket
  warning that asserting `runs-on` / `mounts` / `routes-through`
  / `belongs-to` always lands as a §6 conflict marker was
  softened: §6 fires *only when a competing auto edge already
  exists for that pair*, so a curated `runs-on` on a non-k8s pair
  no probe covers inserts clean (`source: curated,
  conflicts: []`) and is the current right way to assert these
  edges until non-k8s populators ship. Full non-k8s
  `discover_topology` populators stay out of scope for this Task
  (a larger follow-up Initiative).

<!-- bulk roll-up (per-PR bullets authored at release time) -->
- G0.16-T3 backlog prelude on fresh SSE connections (#1321)
- G0.16-T2 gh-rest auth_model reconciliation (Vault-payload discriminator) (#1322)
- G0.16-T1 — async ingest must not crash pod on large specs (#1303 #1323)
- G0.16-T4 probe-route Vault OIDC fingerprint convergence (#1326)
- G0.16-T5 gh/3 catalog label-vs-spec drift opt-in (#1324)
- G0.17-T1 k8s list-op request-shape parity (#1330 #1332)
- accept sddc product alias at create/update validator (#1365)
- G0.18-T6 create_node writes graph_node_history so manual seeds surface to kind=history/timeline (#1372)
- G0.18-T5 tools/list shape-consistency sweep (#1358 #1374)
- G12.3-T3 follow-up — release DB session across verify dispatch + preserve falsy forensics (#1377)
- emit Pydantic-list 422 detail to match OpenAPI schema (#1378)

### Documentation

- **`/mcp` root-mount carve-out documented + `/api/v1/mcp`
  phantom-path confusion closed (G0.18-T9 #1362, RDC #789
  mcp-route).** A new §13 in `docs/codebase/api-shape-conventions.md`
  ("Route-prefix placement: `/api/v1/*` vs the `/mcp` carve-out")
  codifies the convention that every chassis HTTP surface lives
  under `/api/v1/*` while the MCP endpoint is the lone, deliberate
  root-mount at `/mcp` — required by the MCP 2025-06-18 transport
  contract (clients use the bare server URL), RFC 9728
  protected-resource discovery (`resource` claim binds to
  `${BACKPLANE_URL}/mcp`), and the OAuth `aud` audience binding
  the same. The section also pins the tool-name-≠-path-segment rule
  (`query_topology` is a JSON-RPC body parameter, never a URL
  segment — the REST sister is `/api/v1/topology/*`, not
  `/api/v1/query/topology`) and ships a phantom-paths-that-never-
  existed table so future consumer probe scripts stop deriving
  `/api/v1/mcp` from the `/api/v1/*` pattern. One-line cross-links
  from `docs/architecture/mcp.md` (Transport) and
  `docs/cross-repo/mcp-client-setup.md` (Why this doc exists)
  point at §13. No code change; a 308 alias from
  `/api/v1/mcp` → `/mcp` was considered and rejected because the
  OAuth `aud` is bound to `/mcp` so a client following the
  redirect would 401 post-redirect with `invalid_audience`. The
  three v0.8.x dogfood cycles' recurring "mcp-route moved" finding
  was INVALID-as-framed every time; the routes are correct and
  stable since v0.2.0 (#266).

## [0.8.1] - 2026-05-29

### Added

- **Catalog field `spec_info_versions_compatible` for label-vs-spec
  decoupling (G0.16-T5 #1307).** Optional `list[str]` on each
  `ConnectorSpecEntry`. Entries are either glob shapes (`"1.x"`,
  `"9.0.x"`) or PEP 440 specifier sets (`">=1.0,<2.0"`, `"~=1.4"`)
  — any-of semantics across multiple patterns. Documented in
  [`docs/cross-repo/connector-catalog.md`](docs/cross-repo/connector-catalog.md#label-vs-spec-decoupling-spec_info_versions_compatible).
  Companion to G0.16-T6 Finding 22 / Task #1312 H for vmware catalog
  `9.0` vs spec `9.0.0.0` — the new field is available for the
  vmware variant to adopt if Task #1312 chooses approach (b). (#1307)
- **`?envelope=v2` opt-in on the REST topology dependents /
  dependencies endpoints (G0.16-T6 Finding E #1312).** Passing
  `?envelope=v2` returns `{"kind": "dependents", "nodes": [...]}`
  or `{"kind": "dependencies", "nodes": [...]}` matching the MCP
  `query_topology` tool's response shape per
  `docs/codebase/api-shape-conventions.md` §4 (migration goes
  REST-toward-MCP). Default response stays the v0.8.0 bare
  `list[TopologyNode]` so no client breaks. The wider topology
  endpoint set (`path` / `edges` / `timeline` / `diff` /
  `history`) ships in a follow-up Task — those endpoints already
  return typed dict envelopes that need endpoint-specific
  migration decisions.
- **`GET /api/v1/targets?envelope=v2` opt-in returns the unified
  list shape (G0.16-T6 Finding A reference adoption #1312).**
  Pass `?envelope=v2` to receive `{items, next_cursor?}` per
  `docs/codebase/api-shape-conventions.md` §2; omit to keep the
  v0.8.0 bare-list default. The shared helper
  `backend/src/meho_backplane/api/v1/_envelope.py` carries the
  `EnvelopeVersion` type, the `ENVELOPE_QUERY` declaration, and
  the `wrap_v2_envelope` builder so the four sister endpoints
  (`conventions`, `audit/my-recent`, `broadcast/overrides`,
  `connectors`) can opt in via 5-line patches in a follow-up. CLI
  and MCP sister-surface forwarding ships in the same follow-up.
- **Top-level `kind` discriminator on `meho:feed:{tenant_id}`
  entries (G0.16-T6 Finding F #1312).** Every write to the
  per-tenant broadcast stream carries `"kind": "operation"` (audit-
  driven `BroadcastEvent`) or `"kind": "agent_announcement"`
  (`AgentAnnouncementEvent`) per
  `docs/codebase/api-shape-conventions.md` §6. Consumers
  normalize on `kind`; the historical `event_kind` field stays
  serialised on `AgentAnnouncementEvent` for backward
  compatibility with v0.8.0 in-flight stream entries, and pre-
  migration `BroadcastEvent` entries lacking the field on the
  wire infer `kind="operation"` from the model's attribute
  default. Closes the "infer from `op_id`-vs-`activity` field
  presence" anti-pattern RDC #771 Finding 13 catalogued.
- **vmware catalog row adopts `spec_info_versions_compatible:
  ["9.0.x"]` (G0.16-T6 Finding H #1312).** Builds on the
  catalog field shipped via T5 (#1307). The shipped vmware
  entry now declares the band as a belt-and-suspenders
  declaration over the existing PEP-440 prefix-match
  (vmware `9.0` ↔ spec `9.0.0.0` already classifies as
  "exact"). Pairs with T5 which carries the load-bearing
  application for the gh-rest entry where the divergence
  (`"3"` ↔ `"1.1.4"`) blocks ingest without an explicit
  compatibility hint.

### Changed

- **MCP `tools/list` shape-consistency sweep (G0.18-T5 #1358,
  RDC #789 N4).** Schema-pairwise reconciliation of seven
  sibling-tool drifts on the 51-tool MCP surface; the MCP-side
  analogue of the REST/MCP sweep #1312 did for `/api/v1`. None
  breaking — every prior wire name is retained as a deprecated
  alias. The reconciliations:
  - `query_audit.op_class` carries the full broadcast `OP_CLASS_ENUM`
    (incl. `credential_mint`) as a JSON-Schema `enum`, ending the
    "5 vs 6 values" prose-vs-enum drift that made filtering audit
    for freshly-minted credentials undiscoverable.
  - Forward-pagination is named `cursor` everywhere — `query_audit`,
    `query_topology`, `list_targets`, `list_operation_groups`,
    `meho.broadcast.recent`, `meho.broadcast.watch` (canonical).
    `since` (broadcast.recent) and `since_cursor` (broadcast.watch)
    survive as deprecated aliases marked `deprecated: true`;
    passing both forms rejects with `-32602`.
  - `meho.approvals.{get,approve,reject}` accept
    `approval_request_id` (canonical, matching the `<noun>_id`
    convention used by `trigger_id` / `agent_session_id`); the bare
    `id` survives as a deprecated alias.
  - `list_targets.tenant_id` is the canonical cross-tenant scope
    name (matching `meho.connector.*` / `meho.scheduler.create`);
    `tenant` survives as a deprecated alias. `list_targets.tenant_id`
    continues to accept slug-or-uuid (a documented `list_targets`-
    only extension over the admin tools' UUID-only shape).
  - `meho.approvals.list.status` surfaces as a JSON `enum` with
    `default: "pending"` instead of prose-only; pairs with
    `meho.scheduler.list.status`.
  - `meho.scheduler.list.{limit,offset}` and
    `meho.approvals.list.{limit,offset}` declare their defaults
    in-schema (100/0 and 50/0 respectively) so schema-driven MCP
    clients render the documented values.
  - `meho.agent_principals.register.name` carries the documented
    safe-alphabet `pattern` plus `minLength`/`maxLength` at the
    schema layer, matching `meho.agents.create.name`.
  - `list_operation_groups` is keyset-paginated on `group_key`
    (`limit` + `cursor` + `next_cursor`), matching `list_targets`'
    paging shape. REST `GET /api/v1/operations/groups` gains the
    same query params.
  Conventions documented in
  [`docs/codebase/api-shape-conventions.md`](docs/codebase/api-shape-conventions.md)
  §14. Structural regression test at
  `backend/tests/test_mcp_tools_list_shape_conventions.py` pins the
  reconciled vocabulary so a future drift fails CI (#1358).
- **K8s connector list-op request-shape parity — `event` / `service` /
  `ingress` / `configmap` `.list` adopt the `pod.list` input shape
  (G0.17-T1 #1330, RDC #771 Finding 24).** Every namespaced list op
  on the K8s connector now accepts `namespace` XOR `all_namespaces`
  plus `label_selector`, so the operator's "show me all Warning
  events cluster-wide" / "what argocd-labeled services exist across
  the cluster?" question maps to a single
  `{all_namespaces: true, ...}` call instead of an N-namespace
  client-side loop. The `all_namespaces=true` path routes through
  `CoreV1Api.list_X_for_all_namespaces` /
  `NetworkingV1Api.list_ingress_for_all_namespaces`. Backward-compatible:
  existing `{namespace: <X>}` calls keep working unchanged. Anchors
  the new §10 in
  [`docs/codebase/api-shape-conventions.md`](docs/codebase/api-shape-conventions.md)
  (intra-connector list-op request-shape parity). Server-side `limit`
  + `continue_token` paging on service / ingress / configmap deferred
  as a follow-up.
- `POST /api/v1/connectors/ingest` defaults to `async=true` and returns
  `202 Accepted` + a job handle on the non-dry-run path; operators poll
  `GET /api/v1/connectors/ingest/jobs/{job_id}` for completion.
  Real-world vendor specs (the consumer signal was a 7.55 MB / 1275-op
  `vmware/9.0.0.0` ingest that blocked the request thread for ~30 s
  and tripped the kubelet liveness probe → pod restart) no longer
  crash the backplane pod. `dry_run=true` keeps the synchronous shape
  (the parse-only leg is the fast path); pass `async=false` for the
  legacy blocking response on small specs (#1303).
- `composite_l2_missing` error envelope reworded per the
  curated-daily-driver vs OpenAPI-escape-hatch framing in
  [`docs/codebase/api-shape-conventions.md`](docs/codebase/api-shape-conventions.md)
  §1. The human message names the curation gap first, points at the
  L1-wrapper request as the recommended path, and presents the
  `catalog_command` as the escape-hatch recipe rather than the
  remediation path. The structured `extras` (`error_code`,
  `missing_op_ids`, `catalog_command`) are unchanged — agents that
  branch on those fields keep working without migration (#1303).
- **`GET /api/v1/feed?since=` accepts ISO-8601 timestamps
  (G0.16-T6 Finding G #1312).** The SSE feed now mirrors the MCP
  `broadcast.recent` tool's documented contract: operators can
  pass `?since=2026-05-25T10:00:00Z` and let the route normalise
  to a bare-ms Valkey cursor, instead of having to look up the
  Valkey-id of the entry at that instant. Pre-existing Valkey-id
  forms (`1779177600000-0`, `$`) stay accepted unchanged. Closes
  the docs↔impl-disagreement RDC #771 Finding 15 catalogued per
  `docs/codebase/api-shape-conventions.md` §8 (resolution (a),
  extend the impl). Bare dates (no `T`) stay rejected as
  likely-typos.
- **Catalog ↔ TargetCreate enum reconciliation locked in
  structurally (G0.16-T6 Finding B #1312).** RDC #771 Finding 6
  caught the v0.7-era `"sddc"` vs `"sddc-manager"` catalog-vs-enum
  mismatch; subsequent connector renames had already converged
  the catalog to `"sddc-manager"`. The verification regression
  test added in
  `backend/tests/test_operations_ingest_catalog.py` keeps the
  alignment locked in: a future catalog typo or connector rename
  without the matching counterpart edit fails CI rather than
  surfacing as a 422 on the operator's first POST.
- **`preferred_impl_id` accepts the versioned form on both POST and
  PATCH (G0.16-T6 Finding C #1312).** `TargetCreate` and `TargetUpdate`
  validators now treat the canonical `"impl_id-version"` shape
  (e.g. `"nsx-rest-4.2"`) as a valid alternative to the base
  `"nsx-rest"` form, matching `docs/codebase/api-shape-conventions.md`
  §3. The resolver normalizes versioned → base before tie-break
  matching, so an operator typing either form lands on the same
  connector. The unknown-impl 422 lists both forms in
  `valid_impl_ids` for branchable client recovery.
- **CLI commands migrated to the generated typed API client (G0.12).**
  The `agent`, `agent-principal`, `approvals`, `audit`, `broadcast`,
  `connector`, `conventions`, `kb`, `memory`, `migrate`, `retrieval`,
  `scheduler`, `targets`, and `topology` command groups — plus the
  operation verbs — now issue requests through the OpenAPI-generated
  typed transport instead of hand-rolled HTTP. Internal refactor; no
  operator-facing flag or output change. (G0.12-T1–T16, #1262–#1277)

### Fixed

- **SSE feed delivers zero bytes despite stream writes (SEV-1, signal
  draft `sse-feed-delivers-zero-events-despite-stream-writes`)** — a
  fresh `GET /api/v1/feed` or `/ui/broadcast/stream` connection
  defaulted to the Valkey `$` live-tail cursor, which combined with
  the 30 s heartbeat cadence produced 0 bytes for the first 30 s on
  any tenant with no concurrent writes during the window, and
  permanently empty `/ui/broadcast` pages for tenants with 76+
  existing entries on the stream. `_feed_generator` and
  `_ui_feed_generator` now run a backlog prelude
  (`XREVRANGE … COUNT 50`) before the BLOCK loop on fresh `$`
  connections; explicit-replay cursors (`Last-Event-Id`, `since`)
  skip the prelude. Root cause documented in
  `docs/codebase/broadcast.md` as the writer → fanout → consumer
  triage path (#1305 / #1302).
- **gh-rest connector `auth_model` reconciled with `TargetCreate`
  enum (G0.16-T2 #1304).** The v0.8.0 dogfood (consumer signal
  `gh-rest-auth-model-target-vs-connector-mismatch`) caught a
  SEV-1 mismatch between the target schema's `auth_model` enum
  (`{impersonation, shared_service_account, per_user}`) and the
  historical gh-rest connector boundary (which demanded
  `auth_model="github-app"` or `"github-pat"` — neither a legal
  enum value). The fix takes Approach B: the connector now
  inspects the **Vault payload's field shape** to pick the
  upstream credential protocol — `app_id` + `private_key` +
  `installation_id` → App installation-token path; `token` →
  PAT path; neither → typed `github_ambiguous_vault_payload`
  envelope naming both required field sets so operators can
  repair the Vault row without guessing. Targets keep
  `auth_model="shared_service_account"` (the documented runbook
  shape — `docs/cross-repo/github-connector.md` and the new
  `load_github_credentials_from_vault` helper match the doc).
  Mirrors the `vmware-rest-9.0` pattern (target carries the
  identity model; connector reads the protocol from Vault).
  Backwards-compatible for the `evoila-bosnia-gh` shape RDC
  registered against v0.8.0 — the target row already carried
  `shared_service_account` (the only enum value the operator
  could pass), so re-deploying the post-#1304 backplane image
  flips probe + dispatch green without operator action. (#1304)
- **Connector probe — Vault OIDC fingerprint loader converges with dispatch.**
  `POST /api/v1/targets/{name}/probe` and `POST /ui/connectors/{name}/probe`
  now forward the route operator into the resolved connector's
  `fingerprint()`. The four affected connectors (`k8s-1.x`,
  `vmware-rest-9.0`, `sddc-rest-9.0`, `nsx-rest-4.2`) thread that
  operator through the same `vault_client_for_operator(operator)` +
  per-target Vault loader the dispatch path uses, replacing the
  synthesised system operator's placeholder JWT that the v0.8.0 dogfood
  cycle (`claude-rdc-hetzner-dc#771` Finding 4 / signal
  `probe-fingerprint-vault-oidc-malformed-jwt`) surfaced as
  `vault OIDC malformed jwt: must have three parts` on every probe of
  `rke2-infra-k8s`, `rdc-vcenter`, `vcf9-sddc`, and `vcf9-nsx`. The
  `Connector.fingerprint(target, operator=None)` ABC signature gained
  an optional `operator` parameter; the legacy `operator=None`
  fall-back to the system operator stays in place for background
  callers (readiness probe, K8s topology refresh) that have no real
  operator in scope, preserving the locked Option A decision's
  system-call carve-out. (G0.16-T4 #1306)
- **gh/3 catalog ingest no longer fails `spec_label_mismatch` on the
  live upstream spec (G0.16-T5 #1307).** The catalog row's
  `version="3"` is the product-line label (`v3` as github.com itself
  calls it); the upstream OpenAPI description's `info.version` is
  `1.1.4` and grows on every spec edit. Pre-fix the ingest
  validator's verbatim/major-band cross-check refused the pair as
  incompatible majors. The catalog now declares an opt-in
  `spec_info_versions_compatible: ["1.x.x"]` range; the validator
  widens to accept any `info.version` inside the declared band, so
  `1.1.4 → 1.1.5 → 1.2.0` upstream bumps ingest cleanly without a
  catalog edit. The opt-in is per-row — vmware-style catalogs whose
  `version` IS the spec's `info.version` keep the historical strict
  check. Consumer signal:
  [`claude-rdc-hetzner-dc#771` Finding 18](https://github.com/evoila-bosnia/meho-internal/issues/771).
  (#1307)
- **`GET /api/v1/targets` no longer silently masks detail fields
  (G0.16-T6 Finding D #1312).** `TargetSummary` widened to mirror
  the detail-endpoint shape per
  `docs/codebase/api-shape-conventions.md` §5: list rows now
  surface `version`, `tenant_id`, `port`, `fqdn`, `secret_ref`,
  `auth_model`, `vpn_required`, `fingerprint`, `preferred_impl_id`,
  and the `created_at` / `updated_at` / `deleted_at` timestamps.
  The two deliberate omissions (`notes`, `extras`) are operator
  free-form blobs documented in `TargetSummary`'s docstring. A
  structural regression test in
  `tests/test_targets_schemas.py` keeps the contract pinned so a
  future field added to `Target` without the matching summary
  update fails CI.

## [0.8.0] - 2026-05-28

**MVP7 — consolidated post-v0.7 release.** v0.8.0 collapses what
were originally four separate milestones (v0.8 agent-runtime
hardening, v0.9 operator UI, v0.10 audit replay, v0.11 Holodeck)
into one cut, since every line item landed on `main` against the
v0.7 tag without an intermediate release. What's new in the
release window:

- **G11.5 multi-provider seam complete** — per-tenant
  `AgentTier → Model` resolver (T1) routes the three logical agent
  tiers (`triage` / `investigate` / `summarize`) to per-tenant
  Anthropic / OpenAI-compatible (T3 OpenAI + vLLM + Ollama) / AWS
  Bedrock (T2) / VCF Private AI Foundation (T4) backends. T5
  per-identity token budgets + T6 pre-execution budget gate close
  the cost kill-switch leg.
- **G11.6 reference-pattern wave** — R1 tiered triage, R2 operator
  approval gate, R3 closed-loop KB write-back, R4 local-Claude
  cheap-tier triage. All four runnable under `examples/` with CI.
- **G3.11 github-rest connector** — first GitHub REST surface under
  Goal #214: typed connector skeleton (App + PAT auth), curated
  `gh/v3` catalog entry, the first L1 composite
  (`gh.composite.pr_status_summary`), `requires_approval=true` on the
  four destructive write ops, OpenAPI parser support for
  `#/components/responses/*` + `requestBodies/*` refs to ingest the
  GitHub spec cleanly, and an operator on-ramp runbook.
- **G4.4 retrieval enhancements** — `retrieve` accepts
  `metadata_filters` (JSONB containment) and `search_memory` pushes
  RBAC into the substrate metadata_filters rather than re-filtering
  results after the fact.
- **G0.15 v0.7.0 closed-loop dogfood hardening** — ten signals from
  `claude-rdc-hetzner-dc#753` closed: BFF audit-thread (every
  `/ui/*` GET now writes an `audit_log` row), MCP `Mcp-Session-Id`
  issued on `initialize`, probe route fingerprint_failed 500 shape,
  HTML-portal upstream 422 rejection, MCP audit-write column
  hoisting, `/ready` UI-surface enumeration, target version editable
  + wildcard fan-out, JSONFlux handle envelope, UI tenant chip BFF
  wire, UI connectors detail-page Re-probe/PATCH/DELETE distinction.
- **G0.11 substrate hardening** — adopt GitHub merge-queue trigger,
  UUID-audit drift-guard, heavy-pool CI docs.
- **G0.14-T12 K8s topology populator** — first `discover_topology`
  override; closes the v0.6.0 release-body honesty callout.

No breaking changes. The v0.6.0-announced `add_to_memory` `content`
shim continues; v0.9 will land the removal.

### Added

- **BFF audit-thread — every ``/ui/*`` GET writes an audit row
  (G0.15-T7 #1216 / #1240).** Closes the governance product-completeness gap
  ``claude-rdc-hetzner-dc#753`` surfaced in the v0.7.0 closed-loop
  dogfood: an operator browsing five UI surfaces generated **zero**
  ``audit_log`` rows under their ``principal_sub``. Root cause: the
  chassis :class:`AuditMiddleware` skip rule keys on the
  ``operator_sub`` structlog contextvar, and ``UISessionMiddleware``
  resolved the operator into ``request.state`` but didn't bind it into
  structlog — so every read GET through ``require_ui_session`` left
  zero audit footprint. ``require_ui_session`` (now ``async``) calls
  :func:`meho_backplane.ui.audit.bind_ui_view_audit` which binds four
  contextvars: ``operator_sub`` + ``tenant_id`` (lift the skip rule
  and populate the typed columns) plus ``audit_op_id="ui.view.<surface>"``
  / ``audit_op_class="ui_view"`` (the chassis middleware reads both
  into the row's payload). ``op_class="ui_view"`` is a new class
  distinct from agent ``read`` / ``write`` so operators query / prune
  UI page views independently of agent dispatch — the consumer's
  Option B. Target-scoped pages (``/ui/connectors/<name>``) populate
  the typed ``target_id`` column via the existing G0.3-T4 binding in
  :func:`resolve_target`. The single source of truth for the surface
  mapping lives in ``backend/src/meho_backplane/ui/audit.py`` so a
  future surface Initiative cannot accidentally ship a route without
  audit coverage. (#1216)
- **VCF Private AI Foundation backend behind the tier resolver
  (G11.5-T4 #1078).** Closes the **zero-egress** path for the
  G11.5 multi-provider seam. PAIF is OpenAI-compatible at a fixed
  `/api/v1/compatibility/openai/v1/` sub-path (pinned as
  `VCF_PAIF_OPENAI_COMPAT_BASE_PATH`) with an OpenID bearer in the
  `Authorization` header instead of an API key. The wire format
  reuses `OpenAIChatModel` + `OpenAIProvider` from #1077; the
  bearer comes from a **lazy async callable** the openai SDK
  re-resolves on every request — token rotation is transparent
  without rebuilding the resolver. The bundled
  `OidcClientCredentialsTokenProvider` runs the OAuth 2.0
  `client_credentials` grant (RFC 6749 §4.4), caches the access
  token under a `threading.Lock` with a configurable refresh skew
  (default 30 s), surfaces IdP non-2xx / malformed-200 / network
  errors as the typed `TokenAcquisitionError` (the IdP's `error`
  field is included in the message). Six new settings —
  `vcf_paif_base_url` / `vcf_paif_model` / `vcf_paif_oidc_token_url`
  / `vcf_paif_oidc_client_id` / `vcf_paif_oidc_client_secret` /
  `vcf_paif_oidc_scope` — feed `default_vcf_paif_backend_builder()`
  (single-PAIF-endpoint convenience); multi-PAIF deploys use
  `vcf_paif_backend_builder(...)` + `vcf_paif_bearer_provider(...)`
  directly. PAIF registers with `is_saas_egress=False`: an
  air-gapped tenant (`allow_egress=False`) routes every tier to
  PAIF without tripping `EgressViolationError`; a regression that
  mis-flagged it `True` still fails closed (the egress check is
  flag-driven, not URL-parsing). vLLM-equivalent profile
  (`openai_supports_strict_tool_definition=False`,
  `openai_chat_supports_multiple_system_messages=True`) since PAIF's
  chat-completions engine is vLLM (Broadcom techdocs). Cross-repo
  deployer doc at `docs/cross-repo/vcf-paif-deployment.md`. Tenant
  policy persistence + the `AgentModelTier` ↔ `AgentTier` enum
  unification remain the M1 follow-up — the `TODO(G11.5-T2)`
  marker stays. (#1078 / #1208)
- **OpenAPI parser inlines `#/components/responses/*` and
  `#/components/requestBodies/*` refs (G3.11-T7 #1241).** Unblocks
  the GitHub REST spec's live ingest: the upstream spec at
  `raw.githubusercontent.com/github/rest-api-description/main/...`
  uses `#/components/responses/*` refs extensively (1929 hits across
  the spec; every shared envelope — `accepted`, `not_found`,
  `validation_failed` etc — is a responses ref). The parser
  previously raised `UnsupportedSpecError` on the first one,
  short-circuiting the Initiative #1220 G3.11 ingest acceptance.
  `resolve_shallow_ref` now opts into both new buckets via
  `component_responses` / `component_request_bodies` kwargs (mirrors
  the existing opt-in pattern for `component_parameters` from T11
  #501); `parse_openapi` threads all four buckets uniformly. The
  residual `UnsupportedSpecError` envelope is preserved for
  remaining buckets (headers / securitySchemes / links / callbacks /
  examples) so future gaps stay diagnosable. The xfail mark on
  `tests/integration/test_operations_ingest_github.py` (G3.11-T3
  #1223) was removed; the test runs cleanly under
  `MEHO_GH_INGEST_LIVE=1`. (#1241 / #1248)
- **`gh/v3` catalog entry — GitHub REST API on-ramp for L2 ingest
  (G3.11-T3 #1223).** Adds `gh/v3` to the curated connector-spec
  catalog with `impl_id: gh-rest` and `requires_connector_class:
  GitHubRestConnector` (registered by G3.11-T1 #1221). Upstream pins
  the `github/rest-api-description` repo's `main` branch
  (`raw.githubusercontent.com/.../api.github.com.json`, OpenAPI 3.0.3,
  ~700 paths / ~40 tags) — the public release cadence lags by years
  so `main` is the daily-regenerated pin; `spec_info_version: 1.1.4`
  observed against the upstream tip on 2026-05-27. `meho connector
  ingest --catalog gh/v3` (once T1's connector class is registered)
  lands ~700 `endpoint_descriptor` rows; operators flip groups
  (`pulls`, `issues`, `actions`, `repos`) from `staged` to `enabled`
  via `meho operation review`. Live integration test guarded by
  `MEHO_GH_INGEST_LIVE=1` per AC; the operator runbook in
  `docs/cross-repo/github-connector.md` (G3.11-T6) carries the
  end-to-end recipe. (#1223 / #1228)
- **`KubernetesConnector.discover_topology` populator — closes v0.6.0
  signal-13 amendment promise (G0.14-T12 #1201).** First shipped
  override of `Connector.discover_topology` against the K8s connector
  the typed-connector dispatch exercise proved live in v0.6.0. Emits
  one `target`-kinded `NodeHint` for the cluster (properties: server
  `git_version` / `major` / `minor` / `platform` — same payload
  `k8s.about` returns, no extra round-trip), one `namespace` `NodeHint`
  per namespace (properties from `namespace_row` — `status` /
  `age_seconds` / `labels`), one `node` `NodeHint` per cluster node
  (properties from `node_row` — `roles` / kubelet `version` / `kernel`
  / …), plus `belongs-to` `EdgeHint`s from every namespace and every
  cluster node to the target. Pods / services / ingresses /
  deployments / volumes are **explicitly out of scope** at v0.7 — each
  would multiply the per-refresh API-call cost in proportion to
  namespace count, and the v0.7.x deploy hasn't surfaced refresh-cost
  data yet; sibling Tasks land them when justified. The
  [refresh service](backend/src/meho_backplane/topology/refresh.py)
  forwards the per-tenant system operator the scheduler already
  synthesises (`_system_operator` in `topology/scheduler.py`) via
  `inspect.signature`-based detection on the bound `discover_topology`
  method — `Connector` ABC stays unchanged, connectors whose override
  doesn't declare `operator` run verbatim. The deleted regression at
  `backend/tests/test_connectors_topology.py:231` (which asserted
  `KubernetesConnector.discover_topology is Connector.discover_topology`)
  is itself the test that this Task ran. Closes
  `claude-rdc-hetzner-dc#697` signal 13
  (`topology-refresh-no-populator-for-k8s`) and the v0.6.0 GitHub
  release body's "topology populators land in v0.7" honesty callout.
  (#1201 / #1203)

- **Agent runtime — AWS Bedrock Converse backend behind the per-tenant
  resolver (G11.5-T2 #1076).** A tenant policy now routes a logical
  agent tier (`triage` / `investigate` / `summarize`) to AWS Bedrock
  via the existing `ModelResolver` (G11.5-T1 #1075). New
  `bedrock_backend_builder()` constructs a
  `pydantic_ai.models.bedrock.BedrockConverseModel` against a
  `BedrockProvider`; AWS credentials follow boto3's standard chain
  (env vars / IRSA / instance profile / shared profile). The shipped
  `default_bedrock_backends()` registers it under the id
  `bedrock-anthropic` with `is_saas_egress=True` (public Bedrock
  endpoints traverse the public internet); an air-gapped tenant
  brokering Bedrock over AWS PrivateLink registers a sibling
  registration with `is_saas_egress=False`. Capability flags reflect
  Bedrock's Converse API (`tool_format="converse"`, *not* Anthropic-
  native — the two look like "Claude over AWS" from a distance but
  route tool calls through different wire shapes). Prompt caching is
  on for the default Anthropic-on-Bedrock family registration; a
  non-Anthropic Bedrock backend (Nova / Mistral / Cohere) registers
  under a separate id with `supports_prompt_cache=False`. The
  `[bedrock]` extra (boto3) is now pinned alongside `[anthropic]` on
  `pydantic-ai-slim`; both providers stay lazy-imported so an
  Anthropic-only deploy never loads boto3 and an air-gapped Bedrock-
  only deploy never loads the Anthropic SDK. New `BEDROCK_REGION` and
  `BEDROCK_DEFAULT_MODEL` settings; AWS credentials remain owned by
  the boto3 chain rather than surfaced as backplane settings. Persisted
  `AgentDefinition.model_tier` (`standard` / `fast` / `deep`) still
  does not wire to `definition.tier` — the persisted vocabulary and
  the resolver's `AgentTier` vocabulary stay orthogonal until a
  follow-up reconciles them; the resolver remains exercised via
  direct programmatic construction in v0.7.x. (#1076 / #1206)

- **G11.5-T1 per-tenant tier → Model resolver** (#1075 / #1192).
  Introduces `ModelResolver` — a per-tenant policy that maps the
  three logical `AgentTier` values (`triage` / `investigate` /
  `summarize`) to a registered backend builder. Backends register
  by `id` against the resolver and carry capability flags
  (`tool_format`, `supports_prompt_cache`, `is_saas_egress`,
  `openai_supports_strict_tool_definition`, ...). T2 (Bedrock), T3
  (OpenAI-compat), T4 (PAIF) all plug in behind this seam; the
  resolver itself is provider-agnostic. Tenant policy persistence
  + the `AgentDefinition.model_tier` ↔ `AgentTier` enum
  reconciliation remain a follow-up; the resolver is currently
  exercised via programmatic construction.

- **G11.5-T3 OpenAI-compatible backend (OpenAI / vLLM / Ollama)**
  (#1077 / #1204). Adds `openai_backend_builder()` constructing
  `pydantic_ai.models.openai.OpenAIChatModel` against
  `OpenAIProvider`. Default registration lands under the id
  `openai-gpt` with `is_saas_egress=True` (public OpenAI); air-gapped
  vLLM or local Ollama deploys register a sibling id with
  `is_saas_egress=False`. Powers the T4 VCF Private AI Foundation
  bullet above — PAIF reuses this wire format under a fixed
  OpenAI-compatibility sub-path. The `[openai]` pydantic-ai-slim
  extra is now pinned; the SDK stays lazy-imported.

- **G11.5-T5 per-identity token budget + per-op cost source**
  (#1194). Establishes the bookkeeping primitives behind the cost
  kill switch. Per-identity (per-agent or per-operator) budgets are
  persisted; every model invocation deducts the operation's reported
  cost from the current bucket. Cost source is the agent run's
  upstream provider response — there is no hand-tuning. Budgets are
  scoped to the agent or operator identity, not the tenant, so a
  runaway tier-3 agent cannot bleed a tenant's pooled budget.

- **G11.5-T6 pre-execution budget gate + tier degradation + kill
  switch** (#1207). The budget-gate decision runs **before** the
  agent run dispatches: if the next call's projected cost exceeds
  the remaining budget, the run either degrades to a cheaper tier
  (`investigate` → `triage`, `summarize` → `triage`) or kills the
  run (`triage` → terminate). The degradation policy is per-identity.
  Operators see the gate decision on the agent_session audit row.

- **G11.6-T1 R1 tiered-triage reference sample** (#1247). First
  runnable agent pattern under `examples/r1-tiered-triage/`. Demo
  walks a noisy `kubectl get events`-style signal stream through a
  cheap-tier classifier, escalates flagged items to a deep-tier
  investigator, and writes the investigator's structured findings to
  KB via `add_to_knowledge`. The sample wires through the live agent
  runtime (G11.1), the budget gate (G11.5-T6), the model resolver
  (G11.5-T1), and the broadcast feed (G6.1) — every G11 primitive
  exercised end-to-end. Documented in
  `docs/codebase/examples-r1-tiered-triage.md`.

- **G11.6-T2 R2 operator-approval-gate reference** (#1243).
  Companion to R1 demonstrating the `requires_approval=true` flow:
  agent dispatches a write op against a target with an approval
  gate, the run parks at the `approval.requested` broadcast event,
  an operator approves via CLI/MCP/REST or the UI, the run resumes
  on the `approval.decided` broadcast event. Sample at
  `examples/r2-approval-gate/`; guide at
  `examples/r2-approval-gate/README.md`. No new MEHO surface —
  composition over the G11.2 + G11.4 primitives.

- **G11.6-T3 R3 closed-loop KB write-back sample** (#1245).
  Demonstrates an agent reading a tenant convention via
  `search_knowledge`, detecting that the convention is stale against
  observed reality (e.g. a target list that drifted), and writing a
  corrected entry back through `add_to_knowledge` — a closed loop
  where the agent's reasoning improves the same KB it reads. CI
  exercises the loop against an in-process FastAPI app; the guide at
  `docs/codebase/examples-kb-writeback.md` walks the tenant-isolation
  + audit-trail story.

- **G11.6-T4 R4 local-Claude-as-triage + hosted cheap-tier pair**
  (#1244). Captures the "local Claude doing first-pass triage,
  hosted cheap tier doing the deep investigation" pattern — the
  inverse of R1's "cheap cloud tier triages, deep cloud tier
  investigates." Useful for tenants with strong egress posture: the
  triage step runs entirely on the operator's workstation against a
  local Claude (no tenant data leaves the operator); deep
  investigation goes to a hosted cheap tier. Sample +
  end-to-end docs round out the four-pattern G11.6 set.

- **G3.11-T1 GitHubRestConnector skeleton (App + PAT auth)**
  (#1221 / #1231). First GitHub typed connector. Registers
  `GitHubRestConnector` with `impl_id=gh-rest` against the curated
  catalog entry from T3. Two auth models supported: long-lived
  classic PATs (operator-context, for low-blast-radius read ops)
  and GitHub App installation tokens (org-context, for the
  destructive write surface gated by T5's `requires_approval`).
  Connector class declares the four credential families
  (`gh_pat_*` / `gh_app_*`) the credential broker reads.

- **G3.11-T2 GitHub App credential operator runbook** (#1227).
  Step-by-step on registering a GitHub App against an org,
  installing it onto target repos, and storing the App's private
  key + installation id in Vault under the credential broker's
  G3.9 layout. Doc at `docs/cross-repo/github-app-credential.md`.

- **G3.11-T4 `gh.composite.pr_status_summary` — first L1
  composite** (#1237). Composes a single agent-facing op out of
  `pulls.get` + `repos.get-commit-status` + `pulls.list-reviews` +
  `actions.list-workflow-runs-for-pr` — the "is this PR mergeable?"
  question that no single REST call answers. Mirrors the
  composite-recursion pattern from G0.6-T7 #398. First test of the
  pattern against a third-party connector outside vSphere.

- **G3.11-T5 `requires_approval=true` on 4 GitHub write ops**
  (#1236). Gates the four destructive writes — `repos.merge-pr`,
  `repos.delete-branch`, `issues.delete-comment`,
  `actions.cancel-workflow-run` — behind the G11.2 approval queue.
  Agents calling these ops park until an operator approves; ungated
  read ops dispatch directly. Brings the GitHub surface in line with
  the existing approval discipline on vSphere/k8s writes.

- **G3.11-T6 `docs/cross-repo/github-connector.md` operator
  on-ramp runbook** (#1235). First-day recipe for an operator
  enabling the `gh-rest` connector against a target — App vs PAT
  decision tree, credential layout, `meho connector ingest --catalog
  gh/v3` walkthrough, group-by-group enable order (`pulls` →
  `issues` → `actions` → `repos`), the four `requires_approval`
  ops to expect at first dispatch.

- **G4.4-T1 `retrieve` honours `metadata_filters` (JSONB containment)**
  (#1177 / #1246). The `retrieve` op now accepts a
  `metadata_filters` parameter forwarding through to the substrate's
  pgvector + JSONB containment filter (`metadata @> $filters`).
  Agents can scope retrieval to a target product / connector / kind
  without a post-filter pass at the boundary — the substrate does the
  filtering at index time. Backwards-compatible: omit the parameter
  and behaviour is unchanged.

- **G4.4-T2 `search_memory` pushes RBAC into substrate
  metadata_filters** (#1179 / #1256). Migrates the
  `search_memory` RBAC enforcement from a post-query filter on
  results to a substrate-side metadata_filter on the
  `pgvector_memory` index. Same effective security boundary — only
  rows the operator/agent may see come back — but the cost stays
  flat at scale instead of growing with the unfiltered candidate
  set. Same call as the substrate-minimalism principle: smart agent,
  dumb substrate, no DSL.

- **G10.2-T2 KB upload UI — drag-and-drop + bulk + per-file
  progress + `tenant_admin` RBAC** (#1140). The operator UI's KB
  surface gains a drag-and-drop upload zone backed by the existing
  `add_to_knowledge` REST surface, with per-file progress, bulk
  Markdown ingest, and `tenant_admin`-only access. Closes the G10.2
  Initiative by completing the KB write surface alongside the
  read/edit surface that shipped in v0.7.

- **G0.11 — adopt GitHub merge queue (`merge_group` trigger +
  cancel-in-progress guard)** (#769 / #1107). CI workflows now also
  trigger on `merge_group`, so the merge queue (when enabled on a
  PR) re-runs the full test set against the queued merge commit
  before integration. `concurrency.cancel-in-progress: true` on the
  guard prevents stale runs from racing. Lays the groundwork for
  enabling required-merge-queue on `evoila/meho` `main`.

- **G0.11 — UUID audit + drift-guard for `str(uuid)` vs
  `value.hex`** (#1119). Codifies the convention that audit-log
  IDs and request-context UUIDs use the canonical
  `str(uuid.UUID(...))` form (with dashes), not `uuid.UUID(...).hex`
  (no dashes). A migration + CI drift-guard catch regressions where
  a new audit-row writer accidentally emits the dashless form,
  which would silently fail audit-replay's recursive-CTE
  traversal.

- **G0.14-T13 — MCP `initialize` surfaces protocol-version
  mismatch as a structured 400** (#1205). When a client sends an
  unsupported MCP protocol version in `initialize`, the server now
  responds with a structured 400 (`code="protocol_version_mismatch"`,
  `supported`, `requested`) instead of a silent fall-through to the
  default version. Closes signal 15 from the v0.7.0 closed-loop
  dogfood — Claude Code clients hitting a stale server saw a
  half-broken session with no diagnostic.

- **G0.15-T2 — Reject HTML-portal upstreams with structured 422**
  (#1230). The OpenAPI ingest verb now detects HTML responses from
  the upstream spec URL and emits a structured 422 with the upstream
  content-type and first 256 bytes, rather than a confusing JSON
  decode error. Closes signal sub-B from
  `claude-rdc-hetzner-dc#753` — an operator pointing the ingest at
  a portal URL (instead of the raw spec) now sees a useful diagnostic.

- **G0.15-T3 — MCP audit-write column hoisting (findings 1+3+5)**
  (#1229). Lifts three MCP audit fields from the JSON payload into
  typed columns: `mcp_protocol_version`, `mcp_client_name`,
  `mcp_session_id`. Query-by-MCP-client is now indexable. Closes
  three sub-signals at once from the closed-loop dogfood.

- **G0.15-T5 — `/ready` `ui_surface` enumerates
  `UI_SESSION_ENCRYPTION_KEY` + doc-consistency CI gate** (#1232).
  The features block on `/ready` (added in v0.7) now lists the
  `UI_SESSION_ENCRYPTION_KEY` requirement on the `ui_surface`
  entry. A CI gate keeps `/ready`'s reported feature set in lockstep
  with the `docs/configuration.md` configuration matrix — a new
  required env var on a surface forces both updates.

- **G0.15-T6 — Target version editable on
  `TargetCreate`/`TargetUpdate` + wildcard fan-out across typed
  connectors** (#1234). The `version` field on a target row is now
  editable post-create; the resolver applies the v0.6.0
  versioned-beats-wildcard rule across every typed connector
  uniformly (not just vmware-rest). Closes the v0.7.0 dogfood signal
  where bumping a k8s target's `version` from `1.29` to `1.30`
  silently kept dispatching the old version.

- **G0.15-T8 — JSONFlux handle envelope adds `fetch_more` + audit-row
  handle metadata** (#1250). A JSONFlux handle returned from a
  large-payload op now carries a `fetch_more(...)` cursor in the
  envelope, and the corresponding `audit_log` row records the
  handle id + size + retention floor. Operators querying audit can
  see the truncated payload's full source without resorting to
  re-running the op. Closes the v0.7.0 dogfood gap where audit-replay
  on JSONFlux ops was opaque about what got reduced away.

- **G0.15-T9 — UI tenant chip wires to the BFF session, drops
  "(sign in to choose)"** (#1238). The operator UI's tenant chip
  now reads from the BFF-issued session cookie, so the displayed
  tenant matches the one the operator's audit rows land under.
  Closes a confusing v0.7.0 dogfood finding where the chip showed
  a tenant the operator wasn't actually scoped to.

- **G0.15-T10 — Connectors detail page distinguishes Re-probe vs
  PATCH vs DELETE + adds Targets taxonomy** (#1239). The
  `/ui/connectors/<name>` detail page surfaces the three lifecycle
  ops as separate buttons with distinct semantics (`Re-probe`
  re-runs the `about` probe and updates connector metadata; `PATCH`
  edits the connector row; `Delete` removes the connector and its
  targets). Adds a Targets taxonomy with per-target product /
  version / status display.

### Changed

- **Reconcile `gh-rest` catalog/registry version drift** (G3.11-T8 #1249).
  The connector-spec catalog's `version` field is now treated as
  the canonical source for the connector's `impl_id` registration —
  a registry entry whose version doesn't match the catalog gets a
  validator failure at startup rather than silently dispatching
  against a drifted catalog row. Mirrors the discipline from
  vmware-rest where `vmware-rest-9.0` is one impl_id, one catalog
  entry, one registry binding.

### Fixed

- MCP server now issues an `Mcp-Session-Id` response header on every
  successful `initialize` per MCP 2025-06-18 Streamable HTTP §"Session
  Management" rule 1, closing the v0.7.0 release-body's G0.14-T6 #1147
  audit-replay promise that was inert end-to-end. The capture chain
  (header → contextvar → `audit_log.agent_session_id`) already worked;
  what was missing was the issuance half, since spec-conforming MCP
  clients (Claude Code, MCP Inspector) only emit the header when the
  server first sent one. Result: every MCP audit row now carries
  `agent_session_id`, lighting up the G8.2 audit-replay
  `query_audit shape=tree agent_session_id=<id>` flow that the v0.7.0
  rolling dogfood (`claude-rdc-hetzner-dc#753` finding 2) found inert
  on the rke2-infra deploy. (G0.15-T4 #1213 / #1233)

- **G0.15-T1 — `/api/v1/probe/...` route emits a structured
  `fingerprint_failed` 500** (#1210 / #1255). When the probe verb
  cannot fingerprint a target (network failure, auth refusal,
  unexpected schema), the response now carries `code="fingerprint_failed"`
  + the failing step + the upstream's error envelope, rather than a
  bare 500 with a JSON decode error. Operators triaging a failed
  `meho connector probe` get a useful diagnostic.

- **G3.11-T9 — flip `gh.composite.pr_status_summary` integration
  test to live dispatch** (#1257). The xfail mark on the integration
  test came off — the composite now dispatches cleanly against the
  live GitHub API under `MEHO_GH_DISPATCH_LIVE=1`.

- **G3.11-T10 — connector-registry validator asserts the
  `(product, version, impl_id)` triple** (#1259). The validator that
  runs at backplane startup now refuses to start if any registered
  connector class declares a `(product, version, impl_id)` triple
  that collides with another registration. Closes a v0.7.0 latent
  bug where two connector classes registering the same product +
  version with different `impl_id`s would silently shadow each other.

- **G3.11-T11 — Replace `capture_logs` with a monkeypatched
  `LogCapture` in the orphan-class test** (#1258). The `structlog`
  upstream renamed `capture_logs` to a context-manager-only helper;
  the test fixture now monkeypatches `LogCapture` directly, matching
  the rest of the test suite's pattern. Eliminates flake risk on
  newer structlog releases.

### Documentation

- **G0.11 — Update `docs/codebase/devops.md` for heavy-pool runners
  + `-n 6` xdist + PR-mode `--cov`** (#761 / #1110). Captures the
  CI runner-pool right-sizing the parking-lot decision settled in
  v0.7.x. The heavy-pool runner profile (4 vCPU / 8 GB) handles the
  integration-test xdist load; the standard pool stays at 2 vCPU.
  PR-mode coverage runs with `--cov` but main-branch runs strip it
  for speed — the doc now spells out which lane uses which.

## [0.7.0] - 2026-05-27

**MVP6 — agent runtime floor (P1 + P2 + P3) + safety (C1 sanitization)
+ operator web UI surfaces (KB, memory, targets) + v0.6.0
closed-loop dogfood hardening.** v0.7.0 closes the **G11 agentic-ops
floor**: G11.1 lands its final P1 piece (agent runs that park on a
`requires_approval` op now resume on the broadcast decision event,
not only on the REST `/approve+params` express lane), the entire **G11.3
P2 scheduler** ships (cron + one-off + event-outbox triggers, advisory-
lock + SKIP-LOCKED replica-safety, lease/heartbeat + reaper for
restart-durability, admin surface on CLI/MCP/REST), and the whole
**G11.4 C1 sanitization wave** ships in one release window (declarative
policy schema + Tier-1 regex engine, connector-boundary middleware that
captures raw → audit-stores raw → redacts → reduces, Tier-2 Microsoft
Presidio NER for free-text fields, round-trip fixture CI gate +
shadow-mode policy flag, agent-invocation audit row tying per-tool-call
redaction back to the run's model + provider + cost). The **G11.2
identity/RBAC tail** closes the MCP-client on-ramp (Keycloak CIMD docs +
`offline_access` optional scope on the MCP browser-flow client,
dissolving the W6 + W7 walls), plus follow-up polish (TOCTOU honesty in
the identity_ref validator, negative RBAC tests, route-shadow fix,
auto-coverage guard for new tenant.id FKs in TRUNCATE lists,
`approval.expired` as the fourth broadcast lifecycle event).

The **G10 operator web UI** moves from "two surfaces" to "five
production surfaces": KB read + Markdown editor, targets list +
forms + bulk YAML import, memory list + create + scope-promotion +
expiry/bulk. Substrate hardens against the v0.6.0 RDC dogfood
(`claude-rdc-hetzner-dc#697`) across both **G0.13** (auth classifier
DecodeError extension, `/connectors/{id}/review` global-scope fallback,
catalog-driven REST ingest, `add_to_memory` content shim with v0.6.0
breaking-change callout, release-body path-freshness CI gate) and
**G0.14** (T11 error-message-shape convention codified; T1 dispatcher
ambiguity → structured surface; T2 versioned-beats-wildcard resolver
tie-break; T3+T4 target product enum + DELETE route; T5 SSE feed-error;
T6 audit-session capture decoupled; T7 /ready features block; T8
conventions preamble_status; T9 catalog_entry server resolve;
T10 vmware composite L2 pre-flight). No breaking changes in v0.7.0 —
the v0.6.0-announced `add_to_memory` `content` shim continues through
the v0.7.x line; v0.8 will land the removal.

### Added

- **`meho admin keycloak bootstrap-clients` assigns the
  `offline_access` optional client scope to the MCP browser-flow
  client (G0.9.1 follow-up #912).** The verb now reconciles the
  realm's built-in `offline_access` scope onto `meho-mcp-client` as
  an **optional** scope — mirroring the existing default-scopes
  reconcile path (`GET /clients/{uuid}/optional-client-scopes` →
  PUT on miss, skip on hit). Closes the fifth auth-onramp wall (W7)
  hit on the 2026-05-22 RDC dogfood after #790 + #791 shipped:
  Claude Code's MCP client always requests `offline_access` to mint
  a refresh token (OIDC Core §11), and without the scope attached
  Keycloak rejected the authorization request with `invalid_scope`
  (RFC 6749 §5.2) before the user saw a login page. The CLI
  device-code client (`meho-cli`) is deliberately **not** given
  `offline_access` — RFC 8628 device-code clients re-run the device
  dance rather than hold a long-lived refresh token, and a stolen
  device-code refresh token has worse blast-radius than re-prompting
  the operator. `deploy/values-examples/README.md`'s troubleshooting
  matrix grows from four to five walls (W7 added) and the MCP-client
  recipe surfaces the optional scope with the CLI-asymmetry
  rationale. (#912 / #1188)

- **Per-write preamble-inclusion feedback on the conventions write
  surface (G0.14-T8 #1149).** `POST /api/v1/conventions` and
  `PATCH /api/v1/conventions/{slug}` now attach a `preamble_status`
  sub-document to the response when the convention is
  `kind='operational'`. Fields: `included` (whether the slug landed
  in the assembled preamble), `position` (1-based index in the
  packed order, `null` when dropped), `token_count` (the convention
  body's own estimated token cost), and `would_drop_slugs` (the
  full dropped-slug list from this pack — names other slugs the
  write displaced, or includes the just-written slug when it was
  itself dropped). Closes the `claude-rdc-hetzner-dc#697` signal 18
  failure mode: previously an operator who wrote a convention got a
  `201` with no indication whether the row would ever reach an
  agent session; with `preamble_status` the answer arrives in the
  same round-trip. `preamble_status` is `null` on `GET /{slug}`
  (the aggregate budget signal lives on the list response's
  `budget_status`) and `null` for writes against `workflow` /
  `reference` kinds. (#1149 / #1175)

- **`/ready` features block + agent-runtime 503 symmetry +
  `docs/RELEASING.md` post-deploy enablement** (G0.14-T7 #1148).
  `GET /ready` now carries a structured `features` block enumerating
  the four v0.6.0 gated surfaces (`agent_runtime`, `ui_surface`,
  `audit_replay`, `approval_queue`) with `configured: bool`,
  `missing_env: [...]`, and a `docs` reference per feature — one
  GET answers "which features will work out of the box on my
  deploy?". The 503 from `POST /api/v1/agent-principals` when the
  Keycloak admin client is unwired now carries the symmetric
  `/ui/auth/login` shape (three-clause: domain code +
  `KEYCLOAK_ADMIN_URL / KEYCLOAK_ADMIN_CLIENT_ID /
  KEYCLOAK_ADMIN_CLIENT_SECRET` + `docs/cross-repo/keycloak-agent-client.md`),
  exposed as the new `KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL` constant.
  `docs/RELEASING.md` gains §6a "Post-deploy enablement" walking
  operators through each gate. T11-convention-compliant per
  `docs/codebase/error-message-shape.md` (audit table updated).
  Closes `claude-rdc-hetzner-dc#697` signals 16 + 17. (#1173)

- **Agent runtime — `awaiting_approval` runs resume on broadcast
  (G11.1-T9 #1171).** Closes the operator/agent split G11.2 #803
  established. When a `requires_approval` op parks an agent run,
  the wrapped `call_operation` tool now subscribes to the
  per-tenant broadcast feed for `approval.{approved,rejected}` keyed
  on its request id and either re-dispatches with `_approved=True`
  (on approval), surfaces the rejection to the model (on rejection),
  or returns an `awaiting_approval_timeout`-tagged envelope (on
  timeout / broadcast outage). New `backend/src/meho_backplane/agent/
  approval_wait.py` module hosts the read-side primitive
  (`wait_for_approval_decision`) and the agent-facing entry point
  (`resume_or_surface_awaiting_approval`); wraps `call_operation` in
  both `agent/run.py` (T1 default surface) and `agent/toolset.py`
  (T3 resolved surface); `call_operation_with_approval` in
  `operations/meta_tools.py` is the gate-bypass re-dispatch entry.
  Preserves the REST `/approve+params` express lane untouched (the
  human-driven path that re-dispatches inline). Closes the last
  open Task of G11.1 #802. (#1171)

- **Scheduler P2 — cron + one-off triggers fire agent runs
  (G11.3-T2 #1065).** New `scheduler` package + Alembic 0018
  `scheduled_trigger` table host the two simplest P2 trigger shapes
  that fire P1 agent runs: **cron** and **one-off**. Lifespan-owned
  `asyncio` loop on a configurable tick (default 30 s, settable via
  `SCHEDULER_TICK_INTERVAL_SECONDS`). **Replica-safe**: each tick
  claims a process-wide `pg_try_advisory_lock` (mirrors
  `topology/scheduler.py`), then `SELECT ... FOR UPDATE SKIP LOCKED`
  the due rows. The "advance/mark-fired BEFORE invoke" discipline
  plus a conditional `UPDATE` (`WHERE status='active' AND
  next_fire_at=:previous`) is belt-and-braces single-fire even if
  the advisory lock were removed. **Restart-durable**: state lives
  in the row; a long outage fires the trigger exactly once on
  resume and re-anchors to the next scheduled instant — no catch-up
  storm. `AgentInvoker.run()` grew a `trigger` kwarg so the durable
  `agent_run` row's provenance column shows `scheduled` for
  cron/one-off fires. (#1065)

- **Scheduler P2 — event-outbox + drain; agent-run completion fires
  next agent (G11.3-T3 #1129).** Third durable trigger shape
  (event-subscription) so a MEHO-internal event (agent-run reaching
  a terminal state; future: audit predicates, connector alerts)
  durably fires a subscribed agent run, surviving process restarts
  where plain `LISTEN/NOTIFY` would lose the signal. Producer-side
  `publish()` writes the outbox row in the caller's open session
  (same-transaction discipline: a producer rollback discards the
  event); a post-commit `NOTIFY event_outbox_new` fires from a
  short-lived connection as a sub-second wake hint. Durability is
  the outbox row, not the notification. Replica-safe drain via
  `pg_try_advisory_lock` + `SELECT FOR UPDATE SKIP LOCKED`; 10 s
  polled cadence (`EVENT_DRAIN_ENABLED` gate mirrors the
  scheduler) with a parallel asyncpg `LISTEN` task that wakes the
  drain's sleep on every notification. `transition()` in
  `operations/agent_run.py` publishes `agent_run.completed` onto
  the outbox on every terminal-status entry (`succeeded` / `failed`
  / `cancelled`) in the same session as the status write. Subscription
  matcher (`scheduled_trigger.kind='event'` lookup) deferred to
  follow-up once T5 admin surface ships. (#1129)

- **Scheduler P2 — `agent_run` lease/heartbeat + reaper; no run
  silently lost (G11.3-T4 #1125).** Adds `lease_owner` /
  `lease_expires_at` / `in_flight_policy` columns to `agent_run`
  (migration 0025) and five lifecycle helpers (`claim_lease`,
  `heartbeat`, `release_lease`, `snapshot_in_flight_policy`, plus a
  `LeaseLostError` exception). New `agent_run_reaper` background
  task at `backend/src/meho_backplane/agent/reaper.py` —
  `asyncio` lifespan-owned, single-flighted across replicas via
  `pg_try_advisory_lock`, per-tick LIMIT bounded, per-row failure
  isolation. Applies the per-run policy (`fail_into_audit` →
  terminal `failed` + audit row; `resume` → clear lease + audit row
  so dispatcher re-claims). Audit row staged in the same
  transaction as the lifecycle transition. Acceptance contract
  honoured: a run killed mid-flight ends in a terminal audited
  state — never silently lost. (#1125)

- **Scheduler admin surface (CLI + MCP + REST) + durability test
  (G11.3-T5 #1128).** Three transports over the `scheduled_trigger`
  model from #1065. **REST**: `POST/GET/DELETE
  /api/v1/scheduler/triggers`, tenant-scoped via the JWT;
  tenant_admin may pass `tenant_filter` / body `tenant_id` to act
  cross-tenant. `list` is operator-level; `create` / `cancel`
  require tenant_admin. **MCP**: three `meho.scheduler.*` tools
  (list / create / cancel) — picked three verbs over one
  parametric `manage_scheduled_trigger` to match the
  `meho.agents.*` discoverability shape. **CLI**: `meho scheduler
  {list,create,cancel}` cobra tree wraps the REST surface with
  discriminated-union pre-checks for `kind=cron|one_off|event`.
  Service-layer `SchedulerAdminService` is the single code path
  the three transports share (mirrors `AgentDefinitionService`);
  Pydantic schema enforces the discriminated-union invariant at
  the wire so a malformed body surfaces as 422 (not a flush-time
  `IntegrityError`). Cancel uses a conditional `UPDATE` on `status
  IN (active, paused)` so a concurrent scheduler fire cannot race
  it into an invalid state; terminal-fired one-off → 409
  `trigger_already_fired`. Every create/cancel writes
  `op_class='write'`, `op_id='scheduler.{create,cancel}'`;
  `audit_tenant_scope='self'|'other'` records cross-tenant admin
  activity. Closes Initiative #804 (G11.3). (#1128)

- **Redaction — declarative policy schema + Tier-1 regex engine
  (G11.4-T1 #1170).** First Task of Initiative #805 (G11.4 Safety,
  C1). Ships the foundation of the sanitization middleware:
  declarative YAML policy schema, Tier-1 deterministic regex
  engine, and the named-pattern library. The engine is pure and
  side-effect-free (no I/O, no clocks, no logging) so the C1-d
  round-trip CI gate (#1185) can pin determinism; YAML loading
  uses `importlib.resources` mirroring the
  `operations/ingest/catalog.py` precedent. Middleware wiring
  (C1-b, #1180), Tier-2 Microsoft Presidio NER (C1-c, #1184), and
  the round-trip CI gate (C1-d, #1185) land on top of this
  surface. (#1170)

- **Redaction — connector-boundary middleware + manifest into
  audit (G11.4-T2 #1180).** Wires the Tier-1 redaction engine
  (#1170) into `dispatcher._execute_and_audit` so every dispatch
  — user-path **and** agent-path — runs **capture-raw →
  audit-raw → redact → reduce → return**. The caller and LLM see
  only the redacted view; the audit row holds the raw payload plus
  the engine's manifest for forensic recovery. Adds the
  connector-boundary middleware (`meho_backplane.redaction.middleware`)
  + a policy resolver with a six-step specificity ladder (per-
  `connector_id`, per-tenant, per-op → packaged conservative
  default). **Default-safe**: an un-configured connector still
  gets credentials stripped — never pass-through. Migration `0030`
  adds two nullable JSON columns to `audit_log` (`raw_payload`,
  `redaction_manifest`); the resolved policy id mirrors into
  `payload['redaction_policy_id']` for broadcast-event attribution.
  Migration is purely additive (backward-compat guard green).
  (#1180)

- **Redaction — Tier-2 Microsoft Presidio NER for free-text
  fields (G11.4-T3 #1184).** Capability-flagged per policy. A
  `RedactionPolicy` with a `tier2:` block opts into
  `AnalyzerEngine` → `AnonymizerEngine` over policy-flagged
  free-text fields; manifest entries merge into the Tier-1
  manifest with `pattern` prefixed `presidio:` so audit consumers
  can bin Tier-1 vs Tier-2 firings. **Capability-flag guarantee**:
  a Tier-1-only policy never imports `presidio_*` at runtime —
  the middleware checks `policy_uses_tier2(policy)` before any
  Presidio code path runs; `get_engines` does the import + spaCy
  model load lazily on first opt-in. Pins
  `presidio-analyzer==2.2.362` + `presidio-anonymizer==2.2.362`
  (the 2026-03-15 release). CI provisions `en_core_web_sm`
  (12 MB) for the unit lane; the adapter reads
  `MEHO_REDACTION_SPACY_MODEL` so production images can bake the
  heavier `en_core_web_lg` (Presidio's documented default)
  out-of-band. Path-glob matcher (`*` = one segment, `**` = any
  depth) extracted to `redaction/path_glob.py`. (#1184)

- **Redaction — round-trip fixture CI gate + shadow mode
  (G11.4-T4 #1185).** Round-trip fixture suite + harness re-runs
  the active redaction policy against captured raw payloads and
  asserts the engine's output equals `expected.json` exactly —
  same `==` catches both leaks (under-redaction) and
  over-redaction, satisfying Initiative #805's DoD bullet
  "redaction policy round-trips ... enforced in CI". Four
  fixtures cover enforce-mode redact, scoped UUID mask,
  shadow-mode detection, and mask+hash action shapes. Adds
  **shadow / detection-only mode** as a policy-level flag
  (`mode: shadow` in YAML; `RedactionPolicy.mode: Literal["enforce",
  "shadow"]`). The engine still walks the payload and emits the
  full manifest but suppresses in-leaf substitution. No middleware
  re-plumbing or per-call args — flag travels with the policy
  YAML. The **CI gate** is the existing `python-lint-test` job in
  `.github/workflows/ci.yml`: pytest auto-discovers the harness
  file, so a round-trip mismatch blocks merge by branch
  protection without a new workflow step. Meta-tests prove the
  gate fails on both injected-leak and injected-over-redaction
  scenarios. (#1185)

- **Audit — per-tool-call agent-invocation row + policy-replay
  sense (G11.4-T5 #1186).** Per-tool-call dispatcher audit rows
  fired from inside an agent loop are now keyed by the run's id
  on `audit_log.agent_session_id`, and carry the run's `model` /
  `provider` / `cost` snapshot in the JSON payload
  (`agent_model`, `agent_provider`, `agent_cost`). A consumer
  reading one row can attribute it without joining `agent_run`.
  Adds a second audit-replay sense (`replay_policy`) that re-runs
  the recorded `RedactionPolicy` against the row's captured
  `raw_payload` and verifies it reproduces the stored manifest —
  the policy-regression signal the C1-d round-trip CI gate
  (#1185) consumes. Reconstruct-sense replay (`replay_session`,
  G8.2-T3 #1011) is unchanged and verified against agent rows by
  a regression test. (#1186)

- **`approval.expired` broadcast event published from
  `expire_stale_requests` (G11.2-T4 follow-up #1121).**
  `expire_stale_requests` now lifts the decision row's `audit_id`
  onto each returned `ApprovalRequest` as a transient `_audit_id`
  attr, mirroring the pattern create / approve / reject already
  use. The caller publishes one fail-open `approval.expired`
  broadcast event per expired row **after commit** — same
  publish-after-commit invariant the other three lifecycle steps
  follow (#1069). The event's `audit_id` is the real
  `audit_log.id` of the expiry decision row (FK invariant);
  tenant scoping is preserved (event carries the request's
  `tenant_id`, not a sweeper-wide `principal_sub`). Operators
  watching the broadcast feed now see all four lifecycle
  transitions — **pending / approved / rejected / expired** —
  without polling the audit log. `docs/codebase/approvals.md`
  updated: removed this from "Known gaps", added
  `approval.expired` to the broadcast events table. (#1121)

- **Keycloak CIMD onramp documented as the no-pre-registration
  alternative for CIMD-capable MCP clients (G11.2-T6c #1187).**
  Documents enabling Keycloak CIMD (Client ID Metadata Documents)
  as the alternative to the #791 pre-registration path for
  CIMD-capable MCP clients (Claude Code on MCP `2025-11-25+`).
  With CIMD enabled, the `client_id` is the HTTPS URL of the
  client's own metadata document — Keycloak fetches it on the
  fly, so the client needs **no pre-registered client and no
  DCR**, dissolving Wall #6 for those clients.
  `deploy/values-examples/README.md` gains a § CIMD onramp
  section (5 steps: feature flag, three Optional `mcp:tools` /
  `mcp:prompts` / `mcp:resources` scopes with Audience mappers,
  `cimd-profile` + `client-id-metadata-document` executor,
  `cimd-policy` + `client-id-uri` condition, verification recipe).
  Framed as the alternative to #791's pre-registration, **not**
  a replacement — Keycloak < 26.6.0 and non-CIMD MCP clients
  still need the pre-registration path. Stability label
  (experimental) is loud and explicit; the docs link
  [keycloak#45284](https://github.com/keycloak/keycloak/issues/45284)
  so deployers can track GA. Closes #911. (#1187)

- **KB UI read surface — `/ui/kb` search + server-rendered
  Markdown + hover preview (G10.2-T1 + G10.2-T3 #1122).** Ships
  the Knowledge Base UI read surface at `/ui/kb`: search box +
  paginated entry list + ranked search result cards (fused / BM25
  / cosine score pills) + entry detail with server-side Markdown
  render + HTMX hover preview. Server-side Markdown rendering
  via `markdown-it-py` (GFM tables + strikethrough, `html=False`
  to strip raw HTML from kb bodies) + `pygments` syntax
  highlight — no client-side JS highlighter. Pygments CSS
  injected inline in the detail page. Retires the
  `/ui/knowledge` stub; updates `base.html` sidebar, dashboard
  tile, and chassis smoke test to reference `/ui/kb`. Adds
  `markdown-it-py >= 3.0`, `pygments >= 2.18`,
  `python-multipart >= 0.0.12` dependencies. (#1122)

- **KB UI editor modal — CodeMirror 6 + mobile-readable reflow
  (G10.2-T3 #1138).** Adds `POST /ui/kb/editor-preview` HTMX
  live-preview partial (any authenticated operator; renders
  Markdown server-side via `render_markdown`; returns
  `kb/_editor_preview.html` fragment). Adds `POST /ui/kb/new`
  editor save route with `tenant_admin` RBAC gate
  (`_require_tenant_admin`: `load_session` →
  `verify_jwt_for_audience` → `TenantRole.TENANT_ADMIN` check);
  returns 204 + `HX-Redirect` on success, 422 + inline error
  modal on failure. New `kb/_editor_modal.html`: DaisyUI
  `<dialog>` with slug/tags inputs, split CodeMirror 6 pane +
  live-preview column, HTMX-wired hidden textarea. Vendors
  `codemirror-bundle.min.js` (SHA256 `a411a47c…`, 606 KB) as a
  vendored artifact built once offline with esbuild from
  `codemirror@6.0.1` + `@codemirror/lang-markdown@6.3.2`;
  VENDOR.md updated with pinned hash and reproduction recipe.
  Mobile-reflow CSS on `.kb-body` in `detail.html`
  (`overflow-wrap: break-word`, table `display: block;
  overflow-x: auto`, image `max-width: 100%`). (#1138)

- **Memory UI — scope-aware list + detail/edit + delete + tag
  filter (G10.4-T1 #1161).** Replaces the `/ui/memory` chassis
  stub (#866) with the real read + edit + delete + tag-filter
  surface across the five memory scopes (user / user-tenant /
  user-target / tenant / target). Server-side Markdown rendering
  of memory bodies via `markdown-it-py` (commonmark with
  `html=False` for XSS defence) + pygments syntax highlighting
  on code blocks; mirrors the KB UI render precedent (#1122).
  Edit-in-place is gated on `MemoryRbacResolver.can_write`:
  operator edits own user-scoped; tenant-scoped requires
  `tenant_admin`. Cross-user / cross-tenant isolation holds
  (returns 404, never 403, matching the `/api/v1/memory`
  info-leak avoidance). New `resolve_ui_operator` FastAPI
  dependency lifts a full `Operator` with `tenant_role` from the
  BFF session by re-verifying the stored access token through the
  chassis JWT chain; read paths skip the round-trip via
  `build_read_operator`. (#1161)

- **Memory UI — create modal + scope-promotion flow (G10.4-T2
  #1167).** Layers create + scope-promotion onto T1's
  read+edit+delete surface from #1161. "+" on `/ui/memory` opens
  an HTMX-loaded modal with an RBAC-filtered scope selector,
  optional slug, Markdown body textarea with 300 ms-debounced
  server-side preview, expiry picker, and comma-separated tags
  input; submit calls `MemoryService.remember` and HTMX-redirects
  back to the list. Detail page renders a Promote button for
  non-terminal source scopes; the promote modal calls G5.2's
  `MemoryService.promote` which is idempotent against same-scope
  re-runs. Promote handler binds `operator_sub` + `tenant_id` +
  `audit_op_id="memory.promote"` (+ scope/slug/promotion_target_scope)
  so the chassis `AuditMiddleware` writes the canonical audit
  row the AC requires. Module split into `create.py`,
  `promote.py`, `_modal_shared.py` keeps each file under the
  chassis-wide ~600-line cap. (#1167)

- **Memory UI — expiry countdown + recently-expired + bulk
  select/delete/extend (G10.4-T3 #1165).** Adds **server-rendered
  countdown badges** ("expires in 3d 4h") on each memory card,
  with an `hx-trigger="every 60s"` poll on the cards fragment so
  the cue stays fresh without a client-side timer. The refresh
  URL preserves the active scope + tag. Adds the **"Recently
  expired (cleanup pending)" greyed section** below the active
  cards — the bucket is naturally bounded by the G5.2 sweeper
  window (#623), so the operator sees what just rotated out
  before the next 24 h sweeper tick reaps it. Adds **bulk select
  via checkboxes on writable rows** and `POST /ui/memory/bulk`
  for bulk delete / bulk extend-expiry (pre-canned at 1d / 7d /
  30d). HTML5 `form=` attribute associates the checkboxes with
  the toolbar form regardless of DOM nesting. Tenant + RBAC
  re-checked server-side per row; cross-tenant IDs silently fall
  into the "not found" bucket. CSRF inherited from the chassis
  double-submit cookie. Closes Initiative #341 (G10.4) and
  ticks Goal #336 G10.4 line. (#1165)

- **Targets UI — list + detail view + re-probe + recent-ops SSE
  (G10.3-T1 #1172).** Replaces the chassis `/ui/connectors` stub
  with the real read surface for G10.3-T1: sortable +
  filterable targets list, per-target detail page with
  fingerprint card + SSE-live recent-ops + grouped operations
  matrix, and a tenant_admin-gated re-probe action that
  delegates to the same `resolve_connector_or_label` helper the
  REST `/api/v1/targets/<name>/probe` route uses. Recent-ops
  streaming piggy-backs on the existing G10.1 broadcast SSE
  bridge (`/ui/broadcast/stream?target=<name>`) — single-sourced
  SSE plumbing, identical tenant gate. Operations matrix
  consumes the same `(tenant_id IS NULL OR tenant_id = :tenant)`
  scoping `list_operation_groups` uses for the agent surface,
  so the UI's view of available verbs matches what the agent
  sees. (#1172)

- **Targets UI — create/edit forms (DaisyUI modal + HTMX +
  Pydantic + tenant_admin RBAC + CSRF) (G10.3-T2 #1176).**
  Two DaisyUI modals (HTMX-loaded) replace the YAML-edit
  workflow for the common cases — `GET`/`POST
  /ui/connectors/create` and `GET /ui/connectors/{name}/edit` +
  `PATCH /ui/connectors/{name}`. Submit handlers build
  `TargetCreate` / `TargetUpdate` from the form fields and
  delegate to the REST `create_target` / `update_target`
  handlers **in-process**, so the UI and REST surfaces share
  one validation + product-registry-check + audit code path
  (the posture T1's re-probe handler uses). Success → 204 +
  `HX-Redirect: /ui/connectors`; a Pydantic `ValidationError`
  (port outside 1–65535, empty name) re-renders the modal in
  place (422) with per-field messages + echoed values.
  `tenant_admin`-only, gated server-side via
  `resolve_operator_or_403`. The product dropdown is sourced
  from `registered_product_tokens()` — the same set
  `create_target` validates against — so a selectable product
  is always an acceptable product (no dropdown/validator
  drift). (#1176)

- **Targets UI — bulk `targets.yaml` import (paste/upload →
  preview → in-process CRUD) (G10.3-T3 #1181).** Adds the bulk
  `targets.yaml` import UI at `/ui/connectors/import` (work
  item #5 of Initiative #340): paste OR upload a `targets.yaml`
  → server-side `yaml.safe_load` parse → HTMX preview table
  classifying each entry CREATE-vs-UPDATE → confirm → apply
  the plan **in-process** via the existing target CRUD
  (`create_target` for new names, `update_target` for
  existing). **No `/api/v1/targets/import` endpoint** — mirrors
  the client-orchestrated CRUD the `meho targets import` CLI
  (#257) performs. Server-side port of `import.go`'s
  `mapEntry` / `buildLivePlan` so web and CLI imports produce
  byte-identical writes: known keys → columns, unknown →
  `extras` JSONB (merged with an explicit `extras:` block),
  `fingerprint` dropped with a warning, UPDATE emits a sparse
  body (`name` / `product` stripped) so re-imports don't wipe
  omitted columns. Preview→confirm is stateless: confirm
  re-parses + re-classifies against the tenant's current
  targets (a target created between preview and confirm is
  PATCHed, not re-CREATEd into a 409). `tenant_admin`-only,
  CSRF-gated, cross-tenant-isolated. Closes Initiative #340
  (G10.3). (#1181)

- **`next_step` hint on `state=registered` connectors
  (G0.13-T3 #1153).** `GET /api/v1/connectors` now ships a
  `next_step: NextStep | null` field on every row.
  `state="registered"` rows carry a copy/pasteable
  `meho connector ingest --catalog <product>/<version>` verb
  (when the connector-spec catalog #743 has the entry) or a
  manual-mode `meho connector ingest --product ... --version
  ... --impl ... --spec <upstream-openapi-uri>` verb (when it
  doesn't). `state="ingested"` rows set `next_step` to `null` —
  the dispatcher already resolves them. Closes the v0.6.0 RDC
  dogfood signal 11 framing: half-registered connectors fail
  lookup with no in-product hint about what closes the workflow.
  Surfaces the right verb as structured response data instead
  of relying on tribal knowledge. Catalog lookup uses the
  v2-registry's `(product, version)`, not the parser-derived
  shortening, so SDDC (`registry="sddc-manager"` /
  `parsed="sddc"`) resolves to `--catalog sddc-manager/9.0`
  not `--catalog sddc/9.0`. (#1153)

- **Catalog-driven REST ingest — `{catalog_entry}` resolved
  server-side (G0.14-T9 #1182).** `POST /api/v1/connectors/ingest`
  now accepts `{"catalog_entry": "vmware/9.0"}` as an alternative
  to the resolved-quadruple shape. The route resolves the entry
  against the packaged catalog server-side and dispatches through
  the existing ingest path. REST-native agent runtimes (and the
  CLI, refactored) hit one canonical resolution path; the
  discoverability-vs-actionability asymmetry consumer feedback
  flagged is closed. A `@model_validator(mode="after")` on
  `IngestRequest` rejects mixed bodies (`catalog_entry_conflict`)
  and empty bodies (`ingest_request_underspecified`); catalog-side
  failures (`catalog_entry_malformed` / `_not_found` /
  `_typed_connector` / `_templated_upstream`) ship structured 422
  envelopes via `build_catalog_entry_*_detail` helpers in
  `error_envelopes.py`, citing
  `docs/codebase/error-message-shape.md` (T11). CLI refactor:
  `meho connector ingest --catalog <p>/<v>` posts
  `{"catalog_entry": "..."}` directly — no client-side catalog
  fetch + resolve. Removed the now-dead `resolveCatalogEntry` /
  `parseCatalogRef` / `upstreamSpecs` helpers + their tests.
  Closes signal 14 from `claude-rdc-hetzner-dc#697`. (#1182)

- **vmware composite L2 dependency pre-flight (G0.14-T10
  #1183).** Adds a per-composite L2 sub-op pre-flight to
  vmware-rest composites so the operator-visible failure when L2
  isn't ingested is a structured `composite_l2_missing` error
  (per `docs/codebase/error-message-shape.md`) rather than a
  generic `connector_error` wrapping a mid-flight `unknown_op`.
  The new error carries `missing_op_ids[]` +
  `catalog_command="meho connector ingest --catalog vmware/9.0"`
  so an operator (or agent) can act without paging the
  maintainer. Picks Option B (lazy pre-resolve on first call)
  from the three options the issue listed; the rationale is
  documented in `_preflight.py`'s module docstring and
  `docs/codebase/connectors-vmware-rest.md`. Closes signal 20
  (`vmware-composite-ops-depend-on-l2-primitives-not-ingested-by-default`).
  (#1183)

- **`DELETE /api/v1/targets/{name}` + `product` allowed in
  `TargetUpdate` (G0.14-T4 #1164).** Closes the
  "misregistered target cannot be recovered" gap from signal 6 of
  `claude-rdc-hetzner-dc#697`: a single typo at target creation
  previously created a permanent broken row because there was no
  DELETE route and `TargetUpdate` excluded `product`. Adds
  `DELETE /api/v1/targets/{name}` (tenant_admin) — soft-delete by
  stamping `deleted_at`; every read path filters `deleted_at IS
  NULL`; cascade-check on `graph_node.target_id` references
  defaults to 409 + a `?force=true` hint when the target is wired
  into the topology graph. Allows `product` in `TargetUpdate` —
  operator can correct `product='kubernetes'` → `'k8s'`
  in-place; an unknown product yields a structured 422 mirroring
  the `/probe` 501 shape. (#1164)

- **`TargetCreate.product` enum at boot + discoverable 422
  (G0.14-T3 #1166).** Closes the "single typo at target creation
  silently creates a permanent broken row" hole from signal 5 of
  `claude-rdc-hetzner-dc#697`. Ships **both** gold-standard
  layers from the issue body: **Option A** (discoverability) — a
  JSON Schema enum on `TargetCreate.product` populated from the
  live connector registry, injected by a `build_openapi_schema`
  override on `main.app.openapi`. Swagger UI / OpenAPI-driven
  generator tooling surfaces the valid set before the request
  leaves the editor. **Option C** (recovery) — a structured 422
  with `kind`, `product`, `valid_products`, and a `message`
  naming the remediation step + the convention doc. Shape
  complies with the T11 #1141
  `docs/codebase/error-message-shape.md` convention. Both layers
  read from the same `registered_product_tokens()` helper in
  `connectors/registry.py` so they cannot drift. The OpenAPI
  override calls `_eager_import_connectors()` defensively so the
  snapshot script under `cli/api/snapshot-openapi.py` (which
  doesn't run the FastAPI lifespan) renders the correct enum —
  the committed `cli/api/openapi.json` snapshot is updated
  accordingly. (#1166)

- **Release-body path-freshness CI gate + v0.6.0 amendments
  (G0.13-T6 #1159).** Adds a **release-time CI-style gate**
  (`scripts/release/check_release_body_paths.py`) that asserts
  every `/api/v*` path cited in a release body resolves in the
  published OpenAPI snapshot. Sister to the PR-time
  `cli-api-snapshot-freshness` job (#928). Three consecutive
  releases shipped with broken path citations (v0.5.0 missing
  notes; v0.5.1 catalog-vs-dispatch; v0.6.0 audit/replay +
  tenant_conventions + topology/history) — a recurring class of
  defect that deserves a CI gate, not a per-cycle spot-check.
  Amends the v0.6.0 GitHub release body + CHANGELOG `[0.6.0]` to
  cite the shipped paths: `audit/sessions/{session_id}/replay`
  (not `audit/replay`), 3 routes under `/api/v1/conventions` (not
  6 under `tenant_conventions`), `topology/history/{name}` (not
  `topology/history`). Adds two honesty callouts to the v0.6.0
  release body per the 2026-05-26 scope extension: (signal 13)
  topology populators land in v0.7 — substrate ships at v0.6.0
  but no shipped connector overrides `Connector.discover_topology`,
  so `topology/refresh/{target_name}` returns zero-row deltas;
  (signal 15) MCP server silently upgrades
  `initialize.protocolVersion` to `2025-06-18` regardless of
  client request. (#1159)

- **`add_to_memory` `content` alias shim + v0.6.0
  breaking-change callout (G0.13-T4 #1160).** **One-cycle
  deprecation shim** for the `add_to_memory` MCP tool's body
  field. v0.6.x → v0.7.x now accepts both `body` (canonical) and
  `content` (deprecated alias from v0.3.x); `body` wins when both
  are supplied; `content` fires a structured
  `add_to_memory_field_deprecated` warning log line with
  `replacement="body"`, `removal_version="0.7"`, and
  `body_supplied=<bool>` so an operator can distinguish pure
  pinned clients from mid-migration clients. Closes the
  silent-breaking-rename gap RDC reported (consumer signal
  `add-to-memory-content-to-body-silent-rename`; pinned v0.3.x
  clients hit 422 with no migration breadcrumb at v0.6.0). The
  v0.6.0 CHANGELOG opening was retroactively amended to read
  "Breaking changes: 1" with a new `### Changed (breaking)`
  entry naming the rename + the shim grace period + the v0.7
  removal plan; the v0.6.0 GitHub release body amended live via
  `gh release edit v0.6.0`. **v0.7 follow-up note**: the shim
  removal originally scheduled for v0.7 is deferred to v0.8 —
  v0.7.x continues to accept `content`; the removal recipe in
  `docs/RELEASING.md` remains valid for v0.8. (#1160)

- **Error-message-shape convention codified (G0.14-T11
  #1154).** Codifies MEHO's operator-facing error response
  convention at `docs/codebase/error-message-shape.md` — the
  three-clause message shape (code + actionable message naming
  diagnostic values, remediation, and doc reference; optional
  structured `data` payload), the info-leak boundary precedent
  from G0.9.1-T12 #797 (codes in body, values in structlog),
  and the intentionally-bare exception list. Includes a v0.6.0
  audit table tabulating the consumer-cited gold-standard
  surfaces (`/ui/auth/login`, `/probe`, `connectors/ingest`
  `spec_label_mismatch`, `AmbiguousConnectorResolution`) and the
  non-compliant ones (signal 8 dispatcher bare 500, signal 10
  feed bare 500, signal 16 `keycloak_admin_not_configured`)
  with the Task # tracking each per-surface fix. Lands first in
  Initiative #1139 per the user-confirmed ordering — sibling
  Tasks T1 #1142, T5 #1146, T7 #1148 cite the merged doc in
  their respective acceptance criteria. (#1154)

- **Conventions freshness section in consumer `ONBOARDING.md`
  (G7.1 AC8 #1109).** Closes the last remaining gap in #229
  G7.1 DoD: AC8 (freshness behaviour documented in
  consumer-facing `ONBOARDING.md`). Mirrors what
  `docs/codebase/tenant_conventions.md` already documents from
  the backend's perspective — static-at-connect baseline,
  reconnect-to-refresh, conditional `notifications/resources/updated`
  gated on `capabilities.resources.subscribe`. Operator-focused
  framing: starts with "what this means in practice" so a
  `tenant_admin` editing a convention understands why their
  change doesn't reach running sessions until reconnect.
  (#1109)

- **Test infrastructure — auto-coverage guard for new
  `tenant.id` FKs in TRUNCATE lists (G11.2 follow-up #1120).**
  New SQLite-only unit test `backend/tests/test_truncate_list_drift.py`
  walks `meho_backplane.db.models.Base.metadata.tables` for
  every column whose `ForeignKey` targets `tenant.id` and asserts
  the table name appears in both `tests/integration/conftest.py`
  and `tests/acceptance/conftest.py` per-test TRUNCATE lists.
  Closes the recurring drift the Initiative #803 run paid for
  twice (T3 #1052 `agent_permission`, T5 #1069
  `approval_request`) — the next FK-adding PR fails its own
  test, not the next unrelated PR's PG fixture setup. The
  integration conftest inlines the truncate list as a SQL string
  literal, the acceptance one exposes a module-level
  `_TRUNCATE_TABLES` tuple; the guard uses `ast`-based parsing
  to read both shapes without importlib-executing the conftests
  (which would transitively require canary-fixture sibling
  modules + pinned env vars). A third sanity test floors the
  FK-walk at one table so a future metadata-introspection
  regression cannot silently turn the coverage assertions into
  no-ops. (#1120)

- **Test infrastructure — negative RBAC coverage for agent
  grant + approval verbs (REST + MCP) (G11.2 follow-up #1124).**
  Adds gate-layer regression coverage for the G11.2 RBAC
  surfaces wired by #1066 (agent grants) and #1069 (approvals).
  The existing service-layer tests bypass the gate; this PR
  exercises every gated route/tool through the FastAPI
  `TestClient` + MCP dispatch path so a refactor that drops
  `Depends(require_role(...))` from a router or strips
  `required_role=...` from a `ToolDefinition` would fail CI.
  Four new test files, one per surface (REST × MCP × grants /
  approvals), each pinning the tool/route inventory inline so a
  rename or new addition surfaces as a test break. Extends
  `mcp_test_fixtures.isolated_registry` to reload the two new
  MCP tool modules so they register cleanly across the
  fixture-driven test suite. (#1124)

### Changed

- **`call_operation` accepts bare-string `target` alongside dict —
  additive convergence with `query_topology` / `query_audit`
  (G0.13-T2 #1132 / #780 follow-up).** The `call_operation` MCP tool
  and `POST /api/v1/operations/call` REST route now accept the target
  reference in either shape: bare string `"rdc-vault"` (the preferred
  forward shape, matching the read tools) or the existing dict
  `{"name": "rdc-vault"}` (still works, unchanged for callers pinned
  to it). Both reduce to the same dispatch via an internal normaliser.
  The dict shape remains the only one that opens the `fqdn` per-call
  vhost override field. Resolves the "most-cited daily-driver sharp
  edge" `target-shape-inconsistency-across-tools` signal from the
  RDC v0.6.0 closed-loop dogfood (`claude-rdc-hetzner-dc#697`).
  Non-breaking: agents pinned to the dict shape are not affected.
  (#1155)

- Generalise the `tenant_conventions` seed migration: the previously
  shipped `rdc-internal` tenant + 8 consumer-specific operational
  conventions (extracted from one consumer's `CLAUDE.md`) are
  superseded on `upgrade head` by a generic `default` tenant + 2
  illustrative conventions that demonstrate the feature without
  baking in a specific consumer's identity. Operator deploys that
  had already migrated to head with the old seed will see the
  `rdc-internal` seeded rows removed on the next `upgrade head`
  (the `rdc-internal` tenant row itself is preserved; only the
  rows the seed migration authored are removed -- operator-curated
  edits under seeded slugs survive). The consumer-side migration
  template for re-applying the rdc-internal-specific content lives
  in [`docs/architecture/conventions-seed.md`](docs/architecture/conventions-seed.md).
  Closes the operational impact from signal-12 of the v0.6.0
  consumer dogfood: previously, every adopting customer's MCP
  `initialize.instructions` flowed the original consumer's
  operational discipline + repo references into their agent session
  start. (#1137 / #1162)

- **`agents/service.py` polish — identity_ref docstring honesty +
  structured log + validator extracted (G11.2-T9 #1123).** Three
  polish items deferred from G11.2-T7/T8 (#1099 / PR #1108) bundled
  as one post-merge cleanup so `service.py` stays inside its size
  budget before the next G11.2 feature push: (A) drops the incorrect
  "REPEATABLE READ" claim from the validator's docstring, `create()`'s
  inline comment, and `docs/codebase/agent-definition.md` — the
  chassis runs PostgreSQL's default READ COMMITTED, so a revoke that
  lands between the validator's SELECT and the write IS visible to
  the write; the TOCTOU window is small but real, and the
  authoritative gate is G11.3's `run_scheduled` enforcing
  `identity_ref == agent_client_id` under `client_credentials`. (B)
  emits `identity_ref_invalid` structlog `warning` carrying
  `identity_ref`, `reason`, `tenant_id` before each
  `AgentIdentityRefInvalidError` raise — mirroring the
  `agent_definition_create` / `..._update` info events on the happy
  path so operators can grep structured fields for stale-principal
  events. (C) `_validate_identity_ref` moves to
  `agents/identity_ref.py` (re-exported from `service.py` so callers
  don't change); Pydantic-to-ORM mappers (`build_definition_row`,
  `apply_changes`) move to `agents/mapping.py`. `service.py` drops
  from **446 → 367 lines**; `code-quality.py --diff` warnings on
  `service.py` go to **0**. (#1123)

- **Broadcast — shared `xrange + filter` helper between MCP recent
  + UI history (G6.4-T4 #1106).** Collapses the duplicate `xrange`
  + filter + redact-aware parse body that previously lived in both
  the MCP `broadcast.recent` tool and the UI `/ui/broadcast/history`
  route into a single shared module at
  `backend/src/meho_backplane/broadcast/history.py`. T1 (#1091) had
  deferred this unification because the two callers' failure shapes
  differ; this Task lands the helper with that contract divergence
  handled explicitly via two named wrappers. The MCP tool keeps its
  fail-loud contract (`list_recent_events_strict` re-raises
  `RedisError`; the dispatcher maps to `-32603`); the UI route keeps
  its fail-soft contract (`list_recent_events_fail_soft` returns
  `{"events": [], "next_cursor": None}` on `RedisError`; the pane
  renders its empty state, not a 500). T1's full test suite (89
  passed, 10 docker-gated skipped) still passes verbatim; 15 UI
  replay tests pass (one new fail-soft case added); 10 new unit
  tests pin the helper-level contract. (#1106)

### Fixed

- Dispatcher resolver error surfacing — the typed/composite branch
  now mirrors the ingested branch's explicit `no_connector` label
  on `NoMatchingConnector`, and both branches catch
  `AmbiguousConnectorResolution` and surface it as a structured
  `ambiguous_connector` error with the resolver's diagnostic
  message (candidate set + remediation step) in
  `extras.exception_message`. The `/api/v1/targets/{name}/probe`
  route now consults the same shared resolver helper as the
  dispatcher so the two surfaces always agree on whether a
  target's connector resolves; ambiguous probes return 409 with
  the resolver's message. Closes G0.14-T1 signals 7, 8, 19 from
  `claude-rdc-hetzner-dc#697`. (#1142 / #1157)

- `/api/v1/feed` no longer drops to a bare HTTP 500 when the
  broadcast subsystem is unreachable. The SSE generator now catches
  `redis.exceptions.RedisError` (covers `ConnectionError`,
  `TimeoutError`, `ResponseError`) inside the XREAD loop, emits a
  single `event: feed_error` frame carrying a T11-compliant
  `{code, message, doc}` payload
  (`broadcast_subsystem_unavailable`), and closes the stream
  cleanly so the browser `EventSource` reconnect machinery does
  not tight-loop on the failure. The empty-stream case (fresh
  deploy, no events published yet) was already handled — redis-py
  returns `None` for an absent stream key, which falls through to
  the existing heartbeat path. Closes G0.14-T5 signal 10 from
  `claude-rdc-hetzner-dc#697`. (#1146 / #1163)

- **G0.13-T1 auth-invalid-token classifier extended to authlib
  `DecodeError`.** Promotes the decode-stage failure for a non-JWT
  bearer (e.g. `Bearer not-a-real-jwt`) at `/api/v1/health` from the
  residual `invalid_token` to the specific `malformed_jws` 401 detail
  code in `_classify_decode_error`, closing the v0.6.0 dogfood gap
  where the G0.9.1-T12 (#797) classifier only covered claim-stage
  failures. The residual `invalid_token` now applies only to
  non-`DecodeError` failures (`alg: none` via
  `UnsupportedAlgorithmError`, future `JoseError` subclasses,
  post-refresh kid miss). Operators / tooling matching
  `{detail: invalid_token}` for non-JWT bearers now see
  `{detail: malformed_jws}`
  ([#1131](https://github.com/evoila/meho/issues/1131) / #1152).

- **G0.14-T6 audit-replay session-id capture decoupled from
  `MCP_REQUIRE_SESSION_ID`.** `_bind_mcp_session_id` in
  `mcp/server.py` now captures any `Mcp-Session-Id` header the
  client sends into `audit_log.agent_session_id` unconditionally —
  the env var strictly gates enforcement (the missing-header reject)
  and no longer also gates capture. G8.2 audit-replay therefore
  lights up automatically on any default deploy whose MCP clients
  include the header (Claude Code does by default), with no operator
  intervention. A request with no header (or a malformed one) leaves
  `agent_session_id` as NULL — the recursive-CTE replay walks NULLs
  out naturally — replacing the prior fresh-uuid4-per-call fallback
  that polluted the session search surface with one-row "sessions".
  `GET /api/v1/health` gains a new `mcp_session_id_capture` field
  (`"always"` / `"enforced"`) so operators can confirm the deploy's
  capture mode at a glance; `docs/RELEASING.md` documents the
  post-deploy auto-enablement story. Closes G0.14-T6 signal 11 from
  `claude-rdc-hetzner-dc#697`. (#1147 / #1174)

- **Resolver — versioned candidates beat wildcard registrations
  (G0.14-T2 #1156).** The K8s connector self-registers under
  **both** `("k8s", "", "")` (v1 wildcard, written by
  `register_connector` so `get_connector("k8s")` keeps working for
  the `/probe` route) and `("k8s", "1.x", "k8s")` (v2 versioned,
  written by `register_connector_v2` so `connector_id="k8s-1.x"`
  resolves). An unfingerprinted K8s target left both entries in
  play, both scored `(_SPECIFICITY_UNBOUNDED, 0.0)` on the
  specificity ladder because `KubernetesConnector` doesn't
  advertise a `supported_version_range`, priorities tied, and the
  resolver bailed with `AmbiguousConnectorResolution` — a bare 500
  to the operator (T1 #1142 surfaces the diagnostic cleanly going
  forward). Adds a new step 1 `versioned_over_wildcard` to the
  resolver's tie-break ladder: when ≥1 candidate carries a
  non-empty `(version, impl_id)` slot, demote candidates with empty
  slots before the rest of the ladder runs. Conditional — wildcards
  that are the *only* candidate (e.g. `vault` registered v1-only)
  still win. Closes signal 9 from `claude-rdc-hetzner-dc#697`. (#1156)

- **`/api/v1/connectors/{id}/review` two-pass tenant lookup
  (G0.13-T5 #1158).** `GET /api/v1/connectors/{id}/review` now
  applies the same "operator's-tenant + built-ins" scope as
  `GET /api/v1/connectors` — global (`tenant_id IS NULL`)
  connectors stop returning 404 on the daily-driver path (RDC
  v0.6.0 closed-loop validate signal
  `connector-review-tenant-scope-404`). The fix is service-layer
  only: `ReviewService.get_review_payload` falls back to
  `tenant_id IS NULL` when the operator's own-tenant probe misses.
  The route handler stays untouched per the task scope; the PATCH
  edit routes also stay single-pass (the "do tenant_admins edit
  built-ins?" policy decision is intentionally distinct from this
  read-visibility bug). Cross-tenant probes still 404 — the
  fallback only triggers when the caller passes the operator's
  *own* `tenant_id`. The MCP explicit-built-in path
  (`tenant_id=None` argument) keeps its admin-only gate. (#1158)

- **`/api/v1/agents/grants` route reachable — include_router
  order swap (G11.2 follow-up #1169, closes #1168).** Fixes a
  FastAPI route-shadow regression where `GET /api/v1/agents/grants`
  was dispatched to `show_agent(name="grants")` instead of
  `list_grants()` because the agent-definitions router (with
  `GET /{name}`) was registered before the grants router.
  Solution: swap the `app.include_router(...)` order so
  `api_v1_agent_grants_router` runs first — FastAPI route
  precedence is registration order. Restores correct per-role
  behaviour on the list route: `read_only` and `operator` JWTs now
  both surface 403 `insufficient_role` from the grants-list
  `_require_admin` gate (the load-bearing assertion), and
  `tenant_admin` can actually reach `list_grants()`. Folds the
  carve-out `test_read_only_list_route_returns_403` back into the
  parametrised `_GRANT_ENDPOINTS` matrix and removes the
  matrix-level routing-shadow docstring — the workaround
  documented in #1124 is now obsolete. (#1169)

- **Redaction resolver — wildcard register-as-global-override
  semantics restored (G11.4-T6 #1190).** Adds `(None, None, None)`
  as the sixth and final override-lookup step in the redaction
  resolver ladder, restoring the wildcard register-as-global-
  override contract documented in both `register_policy()`'s
  docstring and `docs/codebase/redaction.md`. A wildcard
  `register_policy(policy)` call (no scope kwargs) now shadows
  the packaged default for every `resolve_policy(...)` call.
  More-specific overrides still win per the existing specificity
  hierarchy — adding the sixth step changes no other override
  path. Pre-existing from #1071 (the original wiring); flagged
  as adjacent findings during the PR #1180 and #1185 reviews and
  deferred to this single-Task follow-up. (#1190)

## [0.6.0] - 2026-05-26

**MVP5 — tier-3 standalone connector wave, agent runtime + identity +
approvals (P1+P3), tenant conventions Layer-2 starter, audit replay,
topology history+diff, broadcast meta-tools, and the first operator web
UI surfaces.** This is a substantial minor release that — beyond the
planned v0.6 scope of G3.7 tier-3 connectors (pfSense, gcloud, Hetzner
Robot) and G7.1 tenant conventions — also lands the entire **G11.1
agent runtime** (in-process Pydantic AI loop, definition store,
composition, lifecycle, async invocation surface), the **G11.2 agent
identity / RBAC / approval** plumbing (Keycloak agent clients, per-(
principal, op, target) permission model, durable approval queue,
delegation context for client_credentials autonomous auth), the
**G11.3-T1** scheduler substrate, the **G8.2 audit replay** end-to-end
surface (substrate + REST + MCP + CLI), the **G3.9 + G3.10 live
operator-context Vault credential read** wave (State 2 wiring across
vmware-rest / k8s / nsx / harbor / sddc-manager / vROps / vRLI / Fleet /
vcf-automation), the **G10.0** OAuth2.1 + PKCE BFF auth flow, the first
two **G10 operator-UI surfaces** (broadcast live feed + topology graph),
the **G3.8 Holodeck** typed connector, the **G6.4 broadcast meta-tools**
that make the G7.1 consumer-onboarding CLAUDE.md broadcast-discipline
contract executable, and the **G0.6.1** JsonFluxReducer wiring.
**Breaking changes: 1 — see Changed (breaking) section.**

### Changed (breaking)

- **MCP `add_to_memory` body field renamed `content` -> `body`
  (deferred-callout from G0.9.1-T7, #779).** The rename actually
  landed in this release window (the original task targeted v0.3.2 in
  its CHANGELOG AC, but the release tagged as v0.6.0 due to the v0.3.2
  slip; the breaking-change callout evaporated in the transition).
  Live consumers pinned to the v0.3.1 wire field received a 422
  `missing required field: body` with no migration breadcrumb. v0.6.x
  ships a **one-cycle compatibility shim**: the MCP `add_to_memory`
  tool accepts both `body` (canonical) and `content` (deprecated
  alias). When `content` is supplied, a structured
  `add_to_memory_field_deprecated` warning log line fires with
  `replacement="body"`, `removal_version="0.7"`, and
  `body_supplied=<bool>`. When both fields are supplied, `body` wins.
  **The shim is removed in v0.7** -- agents and SDKs pinned to
  `content` must migrate to `body` before the v0.7 release.
  Acceptance criteria from #779 (v0.3.2 callout) are satisfied
  retroactively here against the actual release window.
  ([#1134](https://github.com/evoila/meho/issues/1134))

### Added

- **Agent runtime (P1) — in-process Pydantic AI tool-use loop
  (G11.1).** New `AgentRun` seam wraps Pydantic AI with bounded
  in-process execution
  ([#808](https://github.com/evoila/meho/issues/808) / #1032), an
  `agent_definition` model + storage + admin CRUD identifies registered
  agents by `identity_ref`, mode, toolset, and budget
  ([#809](https://github.com/evoila/meho/issues/809) / #1035), toolset
  resolution + a handler→agent-tool adapter expose the existing
  meta-tools / connector ops to the loop without per-op re-registration
  ([#810](https://github.com/evoila/meho/issues/810) / #1040), and the
  full invocation surface — sync **and** async (handle / poll / SSE) —
  ships on REST + MCP + CLI
  ([#811](https://github.com/evoila/meho/issues/811) / #1043).
  Agent-invokes-agent composition is depth-capped, budget-aware, and
  audit-linked ([#812](https://github.com/evoila/meho/issues/812) /
  #1042 / #1085) with `ChildRunFinalizer` closing the child
  `agent_run` row when the parent run completes
  ([#1087](https://github.com/evoila/meho/issues/1087) / #1088). The
  `agent_run` record + enforced lifecycle + cancellation are persisted
  end-to-end ([#813](https://github.com/evoila/meho/issues/813) /
  #1031). Session ID = audit linkage throughout.

- **Agent identity + RBAC + approval (P3) (G11.2).** Agent principals
  are first-class Keycloak clients with a `kind=agent`
  principal-discriminator across the audit and policy paths
  ([#815](https://github.com/evoila/meho/issues/815) / #1050,
  follow-up #1089 re-landed a revoke kill switch + `disable_client`
  GET-then-PUT cleanup dropped by the stale-head squash on #1050). A
  resource-server delegation context captures both human initiator and
  acting agent in audit rows and enables `client_credentials`
  autonomous auth ([#816](https://github.com/evoila/meho/issues/816) /
  #1096). The per-(principal, op, target) **permission model** with
  verdict resolution at `policy_gate` replaces the prior unconditional
  pass-through ([#820](https://github.com/evoila/meho/issues/820) /
  #1052). A **durable approval queue** — pending row + resume endpoint
  + two synchronised audit rows — handles long-running operator
  approvals across restarts
  ([#817](https://github.com/evoila/meho/issues/817) / #1086). Agent
  permission **grants are time-bounded** with an expiry sweeper
  ([#819](https://github.com/evoila/meho/issues/819) / #1066). An
  operator-facing **approval surfacing channel** (list / inspect /
  approve / reject) ships on REST + MCP (elicitation URL-mode) + CLI
  ([#818](https://github.com/evoila/meho/issues/818) / #1069). And
  `AgentDefinition.identity_ref` is validated at write-time against the
  agent-principal registry ([#1099](https://github.com/evoila/meho/issues/1099)
  / #1108).

- **Scheduler substrate (P2) (G11.3-T1).** New `scheduled_trigger`
  table + the substrate decision (Option A — roll-our-own over Postgres
  advisory locks + LISTEN/NOTIFY, deferring Celery/APScheduler until
  v0.7 actually fires triggers)
  ([#822](https://github.com/evoila/meho/issues/822) / #1064).

- **Audit replay end-to-end (G8.2).** New `audit_log.agent_session_id`
  column + index + `AuditLog` ORM field
  ([#1017](https://github.com/evoila/meho/issues/1017)) wired through
  the MCP capture of `Mcp-Session-Id` (with
  `MCP_REQUIRE_SESSION_ID` enforcement on production deployments;
  [#1026](https://github.com/evoila/meho/issues/1026)). A recursive-CTE
  `replay_session` substrate + `ReplayNode` shape powers the replay
  ([#1024](https://github.com/evoila/meho/issues/1024)), surfaced as
  `GET /api/v1/audit/sessions/{session_id}/replay` with a 10k
  count-first 413 cap
  ([#1033](https://github.com/evoila/meho/issues/1033)), an MCP
  `meho.audit.replay` admin tool + `meho.audit.*` classifier +
  `query_audit(shape:tree)` shape
  ([#1034](https://github.com/evoila/meho/issues/1034)), and a
  `meho audit replay` + `meho audit query --session-id` CLI verb pair
  ([#1036](https://github.com/evoila/meho/issues/1036)).

- **Tenant conventions + Layer-2 starter — complete (G7.1).** New
  `tenant_conventions` + `tenant_convention_history` tables (Alembic
  migration 0013) with unique `(tenant_id, slug)` and full history
  capture ([#313](https://github.com/evoila/meho/issues/313) / #1029),
  Pydantic schemas + 3 tenant-scoped + RBAC-gated API routes mounted
  at `/api/v1/conventions` (list/create at the collection,
  show/update/delete at `/api/v1/conventions/{slug}`, history at
  `/api/v1/conventions/{slug}/history`;
  [#314](https://github.com/evoila/meho/issues/314) / #1039), `meho
  conventions list / show / create / edit / delete / history` CLI
  verbs with editor integration for `edit`
  ([#315](https://github.com/evoila/meho/issues/315) / #1046),
  session-preamble assembler + MCP `initialize` integration +
  per-slug `meho://tenant/{id}/conventions/{slug}` MCP resource
  ([#316](https://github.com/evoila/meho/issues/316) / #1047), seed
  migration that bootstraps the `rdc-internal` tenant + 8 operational
  conventions extracted from the consumer's CLAUDE.md
  ([#317](https://github.com/evoila/meho/issues/317) / #1045), and a
  `BudgetStatus` surface on `GET /api/v1/conventions` that makes
  `meho conventions list` exit 5 on overflow
  ([#1094](https://github.com/evoila/meho/issues/1094) / #1105).

- **Tier-3 standalone connectors (G3.7) — pfSense / gcloud / Hetzner
  Robot.** Three new typed connectors, each shipping at **State 2**
  per
  [`docs/codebase/connector-release-readiness.md`](./docs/codebase/connector-release-readiness.md):
  - **`pfsense-2.7`** — `SshConnector` subclass with key-only auth
    (password rejected), fingerprint + shell-access probe, registry v2
    ([#844](https://github.com/evoila/meho/issues/844) / #908); 7 read
    ops via `register_typed_operation` parsing `pfctl` / `config.xml`
    into JSONFlux state handles
    ([#847](https://github.com/evoila/meho/issues/847) / #916); CLI
    verbs + MCP review + recorded-fixture / fake-shell E2E + onboarding
    doc ([#850](https://github.com/evoila/meho/issues/850) / #933).
  - **`gcloud`** — `HttpConnector` with `google-auth` ADC +
    impersonation (service-account JSON keys refused on op /
    fingerprint / probe paths), fingerprint + probe, registry v2
    ([#845](https://github.com/evoila/meho/issues/845) / #907); 8 read
    ops (REST via google-auth bearer) via `register_typed_operation`
    + JSONFlux envelope
    ([#848](https://github.com/evoila/meho/issues/848) / #918); CLI
    verbs + MCP review + `respx` E2E +
    `CI_GCLOUD_CREDENTIALS_PRESENT`-gated integration +
    onboarding doc
    ([#851](https://github.com/evoila/meho/issues/851) / #935).
  - **`hetzner-robot-2026-04`** — `HttpConnector` with HTTP Basic
    (Webservice user), no-retry-on-401 (Robot blocks the source IP for
    10 min on repeated 401s), `_post_form` helper, fingerprint + probe,
    registry v2 ([#846](https://github.com/evoila/meho/issues/846) /
    #906); Robot OpenAPI spec ingested, operator-reviewed, and enabled
    as a ~10-op read-only core
    ([#849](https://github.com/evoila/meho/issues/849) / #919); CLI
    verbs + MCP review (401-IP-block warning) + sandbox E2E +
    onboarding doc
    ([#852](https://github.com/evoila/meho/issues/852) / #934).

- **VCF Holodeck typed connector (G3.8).** `HolodeckConnector` skeleton
  + `pwsh` helper ([#1004](https://github.com/evoila/meho/issues/1004)),
  7 typed read ops + read-only `kubectl`
  ([#1005](https://github.com/evoila/meho/issues/1005)), CLI verbs +
  MCP review + recorded-fixture E2E + onboarding doc
  ([#1007](https://github.com/evoila/meho/issues/1007)), with a
  multi-word `kubectl` verb follow-up
  ([#1020](https://github.com/evoila/meho/issues/1020) / #1023).

- **Live operator-context Vault credential read across the connector
  fleet (G3.9 + G3.10) — State 2 for the full fleet.** A shared
  operator-context Vault KV-v2 basic-credentials helper
  ([#954](https://github.com/evoila/meho/issues/954)) and an
  `HttpConnector` auth-surface that threads `Operator` identity
  end-to-end ([#957](https://github.com/evoila/meho/issues/957)) power
  the wave. **`vmware-rest`** now performs the live operator-context
  Vault read with full E2E + onboarding
  ([#963](https://github.com/evoila/meho/issues/963)). The G3.10 wave
  wires the same pattern across **nsx / harbor / sddc-manager**
  ([#972](https://github.com/evoila/meho/issues/972)),
  **vROps / vRLI / Fleet** via the shared `_shared/vcf_auth` loader
  ([#973](https://github.com/evoila/meho/issues/973)),
  **vcf-automation** dual-plane
  ([#971](https://github.com/evoila/meho/issues/971)), and
  **k8s** via `load_kubeconfig_from_vault` (typed handler) with
  recorded + live k3d/Vault E2E
  ([#948](https://github.com/evoila/meho/issues/948) / #975). All ship
  **State 2** per
  [`docs/codebase/connector-release-readiness.md`](./docs/codebase/connector-release-readiness.md):
  fail-closed on empty `operator.raw_jwt` (the system-call carve-out)
  and unset `secret_ref`. Operator recipe at
  [`kubernetes-onboarding.md`](./docs/cross-repo/kubernetes-onboarding.md);
  `per_user` / `impersonation` remain out of scope for k8s.

- **Topology history + diff verbs (G9.3-T3/T4) — companion to v0.5.1
  timeline.** New `meho topology history <name>` +
  `GET /api/v1/topology/history/{name}` + `query_topology(kind=history)`
  expose per-node/edge mutation history
  ([#936](https://github.com/evoila/meho/issues/936)); `meho topology
  diff <ts1> <ts2>` + `GET /api/v1/topology/diff` +
  `query_topology(kind="diff", ts1=..., ts2=...)` returns the net change
  set folded to `created` / `updated` / `removed` with a 1000-row cap
  bounded at the SQL layer
  ([#931](https://github.com/evoila/meho/issues/931), follow-up SQL
  bound #987 / #1000). Cross-Initiative integration suite covers the
  full history surface ([#1027](https://github.com/evoila/meho/issues/1027)).

  > **Groundwork — connector populators land in v0.7.** The topology
  > substrate (graph_node/edge tables, history table, refresh service,
  > diff endpoint, annotate endpoint, UI surfaces) is shipped at v0.6.0,
  > but no shipped connector overrides the base-class no-op
  > `Connector.discover_topology` hook yet, so
  > `POST /api/v1/topology/refresh/{target_name}` returns zero-row deltas
  > for k8s and vmware-rest targets out of the box. Operators populate
  > nodes/edges via `meho topology nodes create` /
  > `topology_create_node` + `meho topology annotate` until per-product
  > populators land. Sister callout to the G10-UI "groundwork — no
  > operator surface enabled yet" framing.

- **Operator web UI — BFF auth flow + first two surfaces (G10.0 / G10.1
  / G10.5).** G10.0 completes the chassis with `/ui/auth/{login,
  callback, logout}` (OAuth2.1 + PKCE) + session middleware +
  `meho-web` Keycloak client
  ([#865](https://github.com/evoila/meho/issues/865) / #959), FastAPI
  `/ui` integration + dashboard + 5 stubs + CSRF + chassis smoke test
  ([#866](https://github.com/evoila/meho/issues/866) / #960). G10.1
  ships the **broadcast live feed view** (`/ui/broadcast` + HTMX SSE
  bridge + 1000-row cap; [#867](https://github.com/evoila/meho/issues/867)
  / #1030), filters by op_class / principal / target / op_id + event
  detail drawer + PII visualization
  ([#868](https://github.com/evoila/meho/issues/868) / #1041), and
  wall-monitor mode (`?wall=1`) + Last-24h replay tab + cross-tenant
  isolation ([#869](https://github.com/evoila/meho/issues/869) /
  #1044). G10.5 ships the **topology UI** — tabular view + node detail
  drawer ([#880](https://github.com/evoila/meho/issues/880) / #974),
  Cytoscape.js graph view (vendored, cose-bilkent layout, 500-node
  cap; [#881](https://github.com/evoila/meho/issues/881) / #1048), and
  dependents/dependencies + path query overlays with 30s polling
  refresh ([#882](https://github.com/evoila/meho/issues/882) / #1049).

- **Broadcast meta-tools (G6.4) — MCP
  `meho.broadcast.{recent,announce,watch}`.** Off-roadmap catch-up that
  makes the G7.1 Layer-2 starter's broadcast-discipline contract
  (before-start / intent / in-flight / completion) actually executable
  for consumer agents. `meho.broadcast.recent`
  ([#1091](https://github.com/evoila/meho/issues/1091) / #1097),
  `meho.broadcast.announce`
  ([#1092](https://github.com/evoila/meho/issues/1092) / #1101), and
  `meho.broadcast.watch` (long-poll `XREAD BLOCK` ≤30s;
  [#1093](https://github.com/evoila/meho/issues/1093) / #1100) now
  ship; the UI history route still uses a separate fail-soft path while
  the shared helper extraction is in flight
  ([#1103](https://github.com/evoila/meho/issues/1103), tracked under
  off-roadmap Initiative G6.4 #1090).

  > **MCP protocol-version negotiation.** The MCP server speaks
  > revision `2025-06-18` and returns it as `protocolVersion` on every
  > `initialize` response, regardless of the version the client sent
  > in the request. Older clients pinned to `2024-11-05` see the
  > server's `2025-06-18` capabilities in subsequent responses (silent
  > upgrade rather than fail-close — MCP spec leaves negotiation to the
  > server). Clients that need a specific protocol revision must check
  > the `initialize.result.protocolVersion` field and adapt.

### Changed

- **`k8s-1.x` typed connector — `shared_service_account` auth model
  live (G3.10-T4
  [#948](https://github.com/evoila/meho/issues/948)).** The default
  [`load_kubeconfig_from_vault`](./backend/src/meho_backplane/connectors/kubernetes/kubeconfig.py)
  now performs the live operator-context KV-v2 read (forwarding the
  operator's Keycloak JWT to Vault's JWT/OIDC auth method, reading the
  `kubeconfig` field at `target.secret_ref`, parsing the YAML into the
  dict shape `kubernetes_asyncio.config.new_client_from_config_dict`
  accepts). `operation call k8s.<op> target=…` executes end to end
  against a real cluster — the rubric **State 2** wiring per
  [`docs/codebase/connector-release-readiness.md`](./docs/codebase/connector-release-readiness.md).
  Fail-closed on empty `operator.raw_jwt` (the system-call carve-out)
  and unset `secret_ref`. Operator recipe:
  [`kubernetes-onboarding.md`](./docs/cross-repo/kubernetes-onboarding.md).
  `per_user` / `impersonation` remain out of scope.

- **JsonFluxReducer wired as the default reducer (G0.6.1).** Real
  `JsonFluxReducer` lands + `set_default_reducer` wiring replaces the
  prior `PassThroughReducer` placeholder
  ([#962](https://github.com/evoila/meho/issues/962)). The JSONFlux
  tree is now vendored under `meho_backplane` (Apache-2.0;
  [#958](https://github.com/evoila/meho/issues/958)) and the seam
  comments / `ForceHandleReducer` shim are removed
  ([#977](https://github.com/evoila/meho/issues/977)).

- **CLI shared dispatch + error-classify helpers extracted
  ([#923](https://github.com/evoila/meho/issues/923)).** Two refactors
  split `meho operation call` and friends into reusable cores so
  connector verbs reuse the same URL resolution + error classification
  ([#937](https://github.com/evoila/meho/issues/937) / #938).

### Fixed

- **Connector credential-cache fail-closed bypass.** A fast-path in
  `harbor` and `sddc-manager` could short-circuit credential
  resolution past the cache guard
  ([#1018](https://github.com/evoila/meho/issues/1018)); the G3.10
  hygiene follow-up adds defense-in-depth fail-closed on the cache
  fast-path itself with an architecture-doc carve-out
  ([#980](https://github.com/evoila/meho/issues/980)).
- **G3.10 `secret_ref` shape guard in `_resolve_secret_ref`** —
  fail-closed on malformed `secret_ref` + normalised fixtures
  ([#1006](https://github.com/evoila/meho/issues/1006)).
- **Harbor robot ops dispatched `Operator`** is now threaded end-to-end
  (production-callable; previously masked by a stale test)
  ([#998](https://github.com/evoila/meho/issues/998)).
- **G3.7 gcloud SA-JSON-key gate** now fires on op / fingerprint /
  probe paths, not just the auth setup
  ([#999](https://github.com/evoila/meho/issues/999)). CLI output
  correctness: honest `iam` footer + `decodeRowsResult`
  absent-vs-empty distinction
  ([#995](https://github.com/evoila/meho/issues/995)).
- **Typed-SSH connectors surface `probe()` / `about()` failures**
  instead of swallowing them
  ([#997](https://github.com/evoila/meho/issues/997)).
- **`ensure_tenant` ON CONFLICT arbitration** now lists every unique
  index, fixing a tenancy race
  ([#983](https://github.com/evoila/meho/issues/983) / #992).
- **Topology `query_diff` fetch bounded at the SQL layer**, not just
  in the Python aggregator
  ([#987](https://github.com/evoila/meho/issues/987) / #1000).
  **Topology soft-delete reachability** reconciled across docs + UI
  overlay parity ([#1068](https://github.com/evoila/meho/issues/1068)).
- **G10.0 UI auth hygiene** — auth-flow fail-closed (`#964`) follow-up
  ([#970](https://github.com/evoila/meho/issues/970)), tightened
  BFF auth-flow tests + MD038 fix
  ([#968](https://github.com/evoila/meho/issues/968)), UI auth 302
  OpenAPI typing + dashboard `aria-label`
  ([#969](https://github.com/evoila/meho/issues/969)).
- **Backplane / broadcast / migration deployments now declare
  `ephemeral-storage` limits** (kubernetes:S6870;
  [#932](https://github.com/evoila/meho/issues/932)).

### Documentation

- **G7.1-T6 Layer-2 starter — `docs/examples/consumer-onboarding/`**
  — `CLAUDE.md`, `ONBOARDING.md`, `README.md` for consumer agents
  inheriting the MEHO operator-contract (broadcast-discipline +
  conventions auto-load); closes #318
  ([#1028](https://github.com/evoila/meho/issues/1028)).
- **G8.2-T8 audit-replay operator runbook**
  ([`docs/codebase/audit-replay.md`](./docs/codebase/audit-replay.md);
  [#1037](https://github.com/evoila/meho/issues/1037)).
- **G3.9-T4 Vault `meho-mcp` templated policy + Keycloak→Vault
  identity deploy runbook**
  ([#953](https://github.com/evoila/meho/issues/953)).
- **G3.9 connector-auth ADR + research + 2026-05-22 roadmap replan**
  ([#951](https://github.com/evoila/meho/issues/951)) — the design
  decision that motivates the G3.9 / G3.10 State 2 wave.
- **ADR for jsonflux vendoring license path** (Option B,
  Apache-2.0; [#955](https://github.com/evoila/meho/issues/955)) —
  the license-compatibility decision behind #958.
- **G0.6.1-T5 `docs/codebase/jsonflux.md`** + sync runbooks +
  reducer-default sweep
  ([#967](https://github.com/evoila/meho/issues/967)).
- **Roadmap refresh** to shipped reality (v0.5.1 latest, v0.6 next)
  ([#1021](https://github.com/evoila/meho/issues/1021)).
- Connector docstring corrections: cache-guard docstrings clarify
  loader is primary gate
  ([#994](https://github.com/evoila/meho/issues/994));
  `PassThroughReducer default` wording corrected post-#753 in
  connectors + operations docs
  ([#996](https://github.com/evoila/meho/issues/996) / #1002).

### Internal (CI / build / quality — no operator-facing change)

- **Go coverage wired to SonarCloud** (completes the Sonar coverage
  story across the polyglot codebase;
  [#952](https://github.com/evoila/meho/issues/952)).
- **`asyncssh` EPL-2.0 dual-license allowed** in the dependency
  license gate ([#976](https://github.com/evoila/meho/issues/976)).
- **xdist subset isolation** — idempotent v2 re-register fixes a
  flake where running a test subset under `-n` could trip
  `already-registered`
  ([#1019](https://github.com/evoila/meho/issues/1019) / #1022).
- **`run_typed_op_registrars` per-boot cost amortised in tests**
  ([#901](https://github.com/evoila/meho/issues/901) / #1025).
- **Registry isolation** — `conftest` snapshots and restores the
  default reducer between tests
  ([#990](https://github.com/evoila/meho/issues/990)); G3.7
  force-handle tests migrated off the `ForceHandleReducer` shim
  ([#991](https://github.com/evoila/meho/issues/991)); de-flaked
  `status --watch` `fakeFeed` tests with request-wait
  ([#1003](https://github.com/evoila/meho/issues/1003)).
- **G11.2-T7 live-Keycloak `client_credentials` integration test +
  reusable testcontainer fixture**
  ([#1098](https://github.com/evoila/meho/issues/1098) / #1104).
- **G8.2-T7 PG replay acceptance suite** —
  tree / tenant / cycle / 413 / broadcast + E2E
  ([#1038](https://github.com/evoila/meho/issues/1038)).
- **Dependency bumps**: `uvicorn[standard]`
  ([#1059](https://github.com/evoila/meho/issues/1059)),
  `python-frontmatter` 1.2.0→1.3.0
  ([#1060](https://github.com/evoila/meho/issues/1060)),
  `ruff` 0.15.13→0.15.14
  ([#1061](https://github.com/evoila/meho/issues/1061)),
  `sqlalchemy[asyncio]`
  ([#1062](https://github.com/evoila/meho/issues/1062)),
  `fastapi` 0.136.1→0.136.3
  ([#1063](https://github.com/evoila/meho/issues/1063)),
  `docker/login-action` 4.1.0→4.2.0
  ([#1057](https://github.com/evoila/meho/issues/1057)),
  `docker/build-push-action` 7.1.0→7.2.0
  ([#1055](https://github.com/evoila/meho/issues/1055)),
  `docker/setup-buildx-action` 4.0.0→4.1.0
  ([#1054](https://github.com/evoila/meho/issues/1054)),
  `docker/metadata-action` 6.0.0→6.1.0
  ([#1053](https://github.com/evoila/meho/issues/1053)),
  `github/codeql-action` 4.35.5→4.36.0
  ([#1058](https://github.com/evoila/meho/issues/1058)),
  `golangci/golangci-lint-action` 9.2.0→9.2.1
  ([#1056](https://github.com/evoila/meho/issues/1056)).

## [0.5.1] - 2026-05-22

**Connector raw-REST ingest on-ramp + topology change-history + UI
chassis groundwork.** This patch lands the Goal #214 connector-spec
catalog (the curated entry point that turns "ingest the vendor's full
REST surface" from tribal knowledge into a discoverable command, on both
the API and CLI), the G9.3 topology change-history substrate (history
tables, diff-on-write capture, a `timeline` query, and retention), and
the first G10 operator-UI chassis pieces (the `ui/` module + BFF session
storage). It also fixes the MCP `tools/list` combinator rejection that
broke Claude Code sessions, and tightens CI (a unit-job time budget,
SonarCloud signature verification + coverage wiring, and a CLI
OpenAPI-snapshot freshness gate). No breaking changes.

### Added

- **Connector-spec catalog — the raw-REST ingest on-ramp (Goal
  [#214](https://github.com/evoila/meho/issues/214)).** A curated,
  server-side catalog mapping `(product, version)` → recommended OpenAPI
  spec source(s) + the registered connector class that covers the version
  label. It ships as package data, is loaded + schema-validated at
  backplane startup (a malformed catalog fails the app-boot smoke), and
  is served read-only at `GET /api/v1/connectors/catalog`
  ([#743](https://github.com/evoila/meho/issues/743) / #917). The
  matching `meho connector catalog list` and `meho connector ingest
  --catalog <product>/<version>` CLI verbs resolve an entry and ingest
  its recommended triple + upstream spec URLs, refusing typed-only and
  fqdn-templated entries with an actionable hint
  ([#915](https://github.com/evoila/meho/issues/915) / #926). This is the
  operator on-ramp for the generic-ingestion (raw-REST) half of the
  two-layer connector model — the answer to the v0.3.0 dogfood's "only 13
  vmware ops?".
- **Topology change history (G9.3).** New `graph_node_history` +
  `graph_edge_history` tables (Alembic migration 0012) capture every
  node/edge mutation ([#900](https://github.com/evoila/meho/issues/900)),
  populated by a diff-on-write hook that also stamps `audit_id` on
  refresh / annotate ([#904](https://github.com/evoila/meho/issues/904)).
  A new `meho topology timeline` verb + `GET /api/v1/topology/timeline` +
  `query_topology(kind=timeline)` expose the history
  ([#909](https://github.com/evoila/meho/issues/909)); a
  `meho topology diff <ts1> <ts2>` verb + `GET /api/v1/topology/diff` +
  `query_topology(kind="diff", ts1=..., ts2=...)` returns the net change
  set between two timestamps folded to `created` / `updated` / `removed`
  (with `--changed-only` to suppress `last_seen`-bump heartbeats and a
  1000-entry hard cap + truncation marker)
  ([#860](https://github.com/evoila/meho/issues/860)). A weekly
  retention prune (`TOPOLOGY_HISTORY_RETENTION_DAYS`, `0` = keep forever)
  bounds growth ([#902](https://github.com/evoila/meho/issues/902)).
- **Operator web UI chassis (G10.0, groundwork — no operator surface
  enabled yet).** A new `ui/` module with a FastAPI BFF mount point,
  Jinja2 base templates, and a Tailwind 4 build pipeline
  ([#897](https://github.com/evoila/meho/issues/897)), plus BFF session
  storage — a `web_session` table with encrypted token custody and RFC
  9700 refresh-token rotation
  ([#903](https://github.com/evoila/meho/issues/903)).

### Fixed

- MCP `tools/list` no longer publishes a top-level `oneOf` / `allOf` /
  `anyOf` in any tool's `inputSchema`. The Anthropic Messages API
  rejects a top-level JSON-Schema combinator in a tool's `input_schema`
  (`400 ... input_schema does not support oneOf, allOf, or anyOf at the
  top level`), and because it validates the whole `tools` array a single
  offender 400'd *every* call in a Claude Code session with the MEHO MCP
  server connected. `query_topology` (top-level `allOf` for its per-`kind`
  conditional requireds) and `meho.topology.unannotate` (top-level
  `oneOf` for its XOR selector) both tripped it. `ToolDefinition.to_wire`
  now strips top-level combinators from the published copy while the full
  schema stays on `inputSchema`, so server-side jsonschema validation
  (the `-32602` rejections for bad argument shapes) is unchanged. Found
  dogfooding from `claude-rdc-hetzner-dc` after its static `.mcp.json`
  wire-up. (#905 / #910)

### Documentation

- Add [`docs/RELEASING.md`](docs/RELEASING.md) — a step-ordered release
  runbook that is the source of truth for cutting a `v*` tag (CHANGELOG
  roll → tag → artefact verification → deploy + smoke)
  ([#914](https://github.com/evoila/meho/issues/914)).

### Internal (CI / build / quality — no operator-facing change)

- Enforce a 10-minute unit-job budget as an early-warning gate against
  CI-perf creep ([#899](https://github.com/evoila/meho/issues/899)).
- Add a CLI OpenAPI-snapshot freshness gate: regenerate the drifted
  `cli/api/openapi.json` snapshot + generated client and fail CI when a
  backend route change leaves them stale
  ([#928](https://github.com/evoila/meho/issues/928) / #929).
- Install `dirmngr` + enable SonarCloud GPG signature verification in the
  quality gate ([#770](https://github.com/evoila/meho/issues/770)); scope
  Sonar to tests + wire coverage with a documented new-code baseline
  ([#920](https://github.com/evoila/meho/issues/920)); resolve coverage
  paths via the `backend/` source root so import coverage isn't reported
  as 0% ([#927](https://github.com/evoila/meho/issues/927)).

## [0.5.0] - 2026-05-22

**VMware Cloud Foundation connector wave + second-cycle dogfood
hardening.** This minor release lands the G3.6 VCF connector fleet
(VCF Operations / vROps, VCF Logs / vRLI, VCF Fleet, VCF Automation,
plus a shared `vcf_auth` substrate), the G5.2 memory-promotion verbs,
harbor operator CLI verbs, and the G0.9.1 hardening of every surface
the 2026-05-21/22 RDC second-cycle dogfood drove against the v0.3.1
deploy — the catalog↔dispatch regression, the `when_to_use`
backfill-on-upgrade gap, memory / ingest / topology polish, and the
full CLI + MCP first-login auth onramp (`auth-config`, the deployer
recipe, the `bootstrap-clients` verb, claim-specific token errors, and
the macOS keyring + device-flow login fixes). CI test-suite performance
was hardened in parallel to keep the unit job under budget as the op
count grew.

### Breaking changes

- **MCP `add_to_memory` argument renamed `content` → `body`**
  ([#779](https://github.com/evoila/meho/issues/779)). Aligns the
  agent-facing memory write surface with `add_to_knowledge` and the
  REST `POST /api/v1/memory` body schema — all three now name the
  field `body`. The tool's `inputSchema` is
  `additionalProperties: false`, so a v0.3.1 client still posting
  `{"content": "..."}` fails loud with JSON-RPC `-32602`
  Invalid Params (not a silent drop).

  Migration: rename the wire field. CLI / REST callers are
  unaffected (REST already used `body`).

  ```diff
  - {"name":"add_to_memory","arguments":{"content":"...","scope":"user"}}
  + {"name":"add_to_memory","arguments":{"body":"...","scope":"user"}}
  ```

### Added

- **`meho vcf-operations` CLI verbs + recorded-fixture E2E + operator
  onboarding doc** (G3.6-T3
  [#837](https://github.com/evoila/meho/issues/837)) — operator-facing
  alias verbs over the 8 enabled vROps read ops (#833), each pre-baking
  `connector_id="vrops-rest-9.0"` so operators don't type it on every
  invocation: `meho vcf-operations about` (versions/current),
  `resource list/get`, `alert list`, `alertdefinition list`,
  `symptom list`, `recommendation list`, `supermetric list`, plus
  `operation search/call` meta-tool wrappers. CLI is pure
  Cobra-over-HTTP — every verb POSTs to `/api/v1/operations/call` on
  the same dispatcher route the agent uses (CLAUDE.md postulate 5;
  vendor logic stays out of the CLI). Recorded-fixture E2E at
  [`backend/tests/test_connectors_vcf_operations_e2e.py`](backend/tests/test_connectors_vcf_operations_e2e.py)
  replays the captured suite-api shape for every enabled op through
  the full `call_operation` stack, asserts the JSONFlux handle path
  on `resource list`, asserts audit rows carry `op_id` + `target_id`
  + `params_hash`, and pins the Basic-auth credential-cache contract
  (no session token, no 401-retry — same posture as Harbor and SDDC
  Manager). Operator wrapper-flip recipe at
  [`docs/cross-repo/vcf-operations-onboarding.md`](docs/cross-repo/vcf-operations-onboarding.md)
  retires `./scripts/vcf-operations.sh`.
- **vROps suite-api spec ingestion + curated read-only v0.5 core**
  (G3.6-T2 [#833](https://github.com/evoila/meho/issues/833)) —
  enables the `VcfOperationsConnector` (#829) for agent dispatch by
  ingesting `docs:vcf-operations-9.0/suite-api.yaml` via the G0.7
  pipeline and curating the 8-op read core that
  `search_operations` / `call_operation` surface:
  `vrops.about` · `vrops.resource.list` · `vrops.resource.get` ·
  `vrops.alert.list` · `vrops.alertdefinition.list` ·
  `vrops.symptom.list` · `vrops.recommendation.list` ·
  `vrops.supermetric.list`. Ships the
  `apply_vrops_core_curation` helper (mirrors NSX / Harbor / SDDC
  precedents — `edit_op(is_enabled=False)` operator-override per
  non-core op, then `edit_group` + `enable_group` cascade), the
  curated 7-group `when_to_use` text + 8-op `llm_instructions`
  blobs, dispatch-smoke + JSONFlux force-handle acceptance tests
  over respx-mocked vROps, and the operator runbook at
  [`docs/cross-repo/g36-vrops-canary.md`](docs/cross-repo/g36-vrops-canary.md).
  Write ops (custom-group / maintenance-mode set / alert-ack) stay
  `is_enabled=False` per the Initiative #369 out-of-scope list.
- **vRLI 9.x read-only v0.5 core curation** (G3.6-T5
  [#834](https://github.com/evoila/meho/issues/834)) —
  `connectors/vcf_logs/core_ops.py` ships `VRLI_CORE_OPS` /
  `VRLI_CORE_GROUPS` / `apply_vrli_core_curation` enabling exactly
  **7 read-only operations** across 5 groups against the
  `vrli-rest-9.0` connector triple after G0.7 spec ingestion of
  `vcf-logs-9.0/api-v2.yaml`: `vrli.about`
  (`GET /api/v2/version`), `vrli.event.query`
  (`GET /api/v2/events/{constraints}` — JSONFlux-handle-shaped),
  `vrli.aggregated.query`
  (`GET /api/v2/aggregated-events/{constraints}`),
  `vrli.field.list` (`GET /api/v2/fields`), `vrli.host.list`
  (`GET /api/v2/hosts`), `vrli.content.pack.list`
  (`GET /api/v2/content/contentpack/list`), and `vrli.alert.list`
  (`GET /api/v2/alerts`). The `classify_vrli_op` path-prefix
  classifier rejects non-`GET` methods so write ops never land
  under a curated group; `apply_vrli_core_curation` mirrors the
  Harbor + NSX precedents (audit-log-driven operator-override
  exclusion so `enable_group`'s cascade skips non-core ops in
  curated groups). Operator runbook at
  [`docs/cross-repo/g36-vrli-canary.md`](docs/cross-repo/g36-vrli-canary.md).
- **`VcfOperationsConnector` skeleton** (G3.6-T1
  [#829](https://github.com/evoila/meho/issues/829)) — `HttpConnector`
  subclass registered under
  `(product="vcf-operations", version="9.0", impl_id="vrops-rest")`.
  HTTP Basic auth on every request (vROps' `/suite-api/api/*` surface
  is stateless — no session token); optional `auth-source` query
  parameter on authenticated requests when `target.auth_source` is set,
  routing the Basic challenge to a non-local identity domain (vIDM, AD
  realm name, etc.). Auth-model boundary gate accepts
  `shared_service_account` / the enum member / `None` (pre-G0.3
  sentinel) and rejects everything else with `NotImplementedError`
  naming the target + mode. `fingerprint()` against
  `GET /suite-api/api/versions/current` lifts `releaseName` →
  `version`, `buildNumber` → `build`, and `humanlyReadableReleaseName`
  → `extras` when present; transport / status failures return
  `reachable=False` with structured `extras["error"]`. `probe()`
  delegates to `fingerprint()` — vROps has no dedicated `/health`
  endpoint. Shares the `connectors/_shared/vcf_auth.py` scaffolding
  ([#841](https://github.com/evoila/meho/issues/841)) for the Basic
  header, auth-model predicate, credentials cache, and Vault loader
  stub with the sibling vRLI #830 + Fleet #831 skeletons. Operations
  ship in G3.6-T2 (#833) via G0.7 spec ingestion against the vROps
  `/suite-api` OpenAPI spec.
- **`meho admin keycloak bootstrap-clients` CLI verb** (G0.9.1-T11
  #791). Idempotently provisions the realm-side prerequisites the
  2026-05-21 RDC dogfood proved are the single highest-friction
  install step: the public `meho-cli` device-code client + the
  public `meho-mcp-client` browser-flow client (PKCE), **5 protocol
  mappers on each** (`audience-meho-backplane`, `meho-mcp-audience`,
  `tenant-id`, `tenant-role`, `groups-claim`), **4 default client
  scopes on each** (`basic`, `roles`, `web-origins`, `acr` — the
  `basic`/`sub` Keycloak 25+ gotcha is the load-bearing one), plus
  the `meho-admins` group and an admin user with a password. Encodes
  the 5-step recipe from
  [`deploy/values-examples/README.md` § Auth onramp recipe](deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp)
  so a fresh `helm install`-shaped deploy gets a working
  authenticated CLI + MCP onramp in one verb instead of ~2.5 hours
  of console clicking. Re-runs are idempotent (`[skip]` /
  `[updated]` per resource; never duplicates). Confidential clients
  (`meho-backplane`) and silent-password-rotation on user re-creates
  are explicitly refused. Passwords flow via env vars
  (`KEYCLOAK_ADMIN_PASSWORD`, `KEYCLOAK_ADMIN_USER_PASSWORD`) or
  stdin — never argv. Stdlib-only HTTP client; no Keycloak Go SDK
  added to the dep graph.
- **`meho.topology.create_node` MCP verb** (tenant_admin, `op_class="write"`)
  for manual `graph_node` seeding — closes the empty-tenant bootstrap
  gap surfaced by the 2026-05-21 RDC second-cycle dogfood (Signal #14).
  A fresh tenant has zero nodes; `meho.topology.annotate` previously
  required both endpoints to already exist as `graph_node` rows, and
  the only node-creating path was the CLI verb
  `meho topology refresh <target>` — unreachable from an MCP session.
  The new verb is idempotent on `(tenant, kind, name)`, writes one
  audit row (`op_id="topology.create_node"`,
  `method="CREATE_NODE"`) and one broadcast event per call. The verb
  is also the canonical path for curated inner-graph nodes the probes
  cannot derive (vault-role, keycloak-realm, externally-managed
  principals) ([#778](https://github.com/evoila/meho/issues/778)).
- **VCF Fleet spec-ingest + 8-op read core** (G3.6-T8
  [#890](https://github.com/evoila/meho/issues/890)) — enables the
  `VcfFleetConnector` (#886) for agent dispatch by ingesting the Fleet
  spec via the G0.7 pipeline and curating an 8-op read core that
  `search_operations` / `call_operation` surface. **Dispatch + catalog**
  state; production execution against per-target Vault credentials is
  tracked under [#214](https://github.com/evoila/meho/issues/214). Write
  ops stay `is_enabled=False` per Initiative #369.
- **VCF Automation dual-plane spec ingestion + 11-op read core**
  (G3.6-T11 [#892](https://github.com/evoila/meho/issues/892)) — enables
  the `VcfAutomationConnector` (#885) for agent dispatch across both VCFA
  planes; 11 read ops curated and surfaced by `search_operations` /
  `call_operation`. **Dispatch + catalog** state; loader wiring tracked
  under [#214](https://github.com/evoila/meho/issues/214).
- **Three more VCF connector skeletons registered** — `HttpConnector`
  subclasses with `fingerprint()` + `probe()` but **no dispatchable ops
  until their curation Tasks land** (registered-not-ingested state):
  - **VcfLogsConnector** `vrli-rest-9.0` — session-token auth +
    401-retry-once (G3.6-T4,
    [#887](https://github.com/evoila/meho/issues/887)); ops via the vRLI
    read core (#834).
  - **VcfFleetConnector** — HTTP Basic (`admin@local`, no SSO) +
    wrapper-verified probe (G3.6-T7,
    [#886](https://github.com/evoila/meho/issues/886)); ops via #890.
  - **VcfAutomationConnector** — dual-plane auth + vhost routing
    (G3.6-T10, [#885](https://github.com/evoila/meho/issues/885)); ops
    via #892.
- **Shared `connectors/_shared/vcf_auth.py` substrate + recorded-fixture
  refresh tool** (G3.6-T13 [#841](https://github.com/evoila/meho/issues/841)
  / #884) — common Basic / session auth scaffolding, auth-model
  predicate, credentials cache, and Vault loader stub shared across the
  VCF connector skeletons, plus the tool that refreshes the recorded
  HTTP fixtures the connector E2E suites replay.
- **Operator CLI alias verbs for three more connectors** — pure
  Cobra-over-HTTP wrappers that pre-bake the `connector_id` and POST to
  `/api/v1/operations/call` (the same dispatcher route the agent uses;
  vendor logic stays out of the CLI per CLAUDE.md postulate 5), each
  with a recorded-fixture E2E and an onboarding doc:
  - **`meho vrli`** over the vRLI read core (G3.6-T6,
    [#896](https://github.com/evoila/meho/issues/896)).
  - **`meho fleet`** over the Fleet read core (G3.6-T9,
    [#894](https://github.com/evoila/meho/issues/894)).
  - **`meho vcf-automation`** over the VCFA dual-plane core, with
    `--fqdn` plane selection (G3.6-T12,
    [#895](https://github.com/evoila/meho/issues/895)).
- **`meho harbor` operator CLI alias verbs** over the `harbor-rest-2.x`
  op surface, with container E2E + onboarding doc (G3.5-T10
  [#622](https://github.com/evoila/meho/issues/622) / #768).
- **Memory promotion** — `POST /api/v1/memory/{scope}/{slug}/promote`
  (idempotent) + the `meho.memory.promote` admin meta-tool (G5.2-T4
  [#626](https://github.com/evoila/meho/issues/626) / #764), and the
  `meho promote` CLI verb with exit-code mapping + E2E smoke (G5.2-T5
  [#627](https://github.com/evoila/meho/issues/627) / #784).

### Changed

- **`meho.topology.annotate` tool description** now states the
  bootstrap precondition ("both endpoints must already exist as
  `graph_node` rows") and names the remediation paths
  (`meho.topology.create_node` for MCP-only seeds; `meho topology
  refresh <target>` for probe-driven seeds). An agent reading the
  tool description alone can now recover from the
  `-32602 no graph_node matched <name> in this tenant` failure mode
  ([#778](https://github.com/evoila/meho/issues/778)).
- **MCP `meho.broadcast.overrides.set` response now exposes
  `override_id` at top level**
  ([#779](https://github.com/evoila/meho/issues/779)) — symmetric
  with the `override_id` argument of
  `meho.broadcast.overrides.remove`. The nested `override` envelope
  is preserved (`response.override.id == response.override_id`), so
  v0.3.1 clients reading `.override.id` keep working; new clients
  can read `.override_id` directly and hand it to `.remove` without
  walking the envelope.

### Fixed

- `search_memory` now returns real `created_at` / `updated_at` for
  each hit instead of the `1970-01-01T00:00:00Z` epoch placeholder
  that v0.3.1 surfaced. The retrieval substrate's `RetrievalHit`
  carries the persisted `documents` row timestamps through to memory
  search projections, so the read path matches what `add_to_memory`
  and direct recall return for the same row (#776).
- Structured ingest error envelopes on the MCP path —
  `meho.connector.ingest` now maps `VersionMismatchError` and
  `UncoveredVersionLabel` to JSON-RPC `-32602 Invalid Params` with a
  structured `error.data` payload (`requested_version`,
  `spec_info_versions`, registered-class ranges) instead of the prior
  `-32603 "internal error: VersionMismatchError"`. Detail builders are
  shared with the REST 422 envelope so the wire shapes can't drift.
  (#777)
- Reconcile `GET /api/v1/connectors` with the dispatcher resolve path
  so no listed `connector_id` is unresolvable. Drops stale-rename DB
  rows (e.g. pre-`k8s` `kubernetes-asyncio-1.x` survivors from G3.2
  #320) whose emitted `connector_id` cannot round-trip through
  `parse_connector_id` + `connector_exists`. Adds `ConnectorListItem.state`
  (`"ingested"` for DB-backed dispatchable rows, `"registered"` for
  class-side-only opless entries) so an agent / operator browsing the
  catalog distinguishes a connector the dispatcher will resolve from one
  that's registered but not yet dispatchable. De-circularises the
  `UnknownConnectorError` message to no longer point at the listing as
  the remediation for a listed-but-unresolvable id. Closes Signal #6
  from the 2026-05-21 RDC v0.3.1 dogfood
  ([#773](https://github.com/evoila/meho/issues/773)).
- Complete `/api/v1/auth-config` with a public `cli_client_id` field
  (chart-wired via `config.keycloakCliClientId` / env
  `KEYCLOAK_CLI_CLIENT_ID`) and fix the `meho login` CLI's discovery
  mapping — the CLI now drives the device-code `client_id` from
  `cli_client_id` instead of mis-mapping `audience` (the confidential
  resource-server identifier, which Keycloak rejects for device-code
  with `401 unauthorized_client`). Stale `meho login --help`
  ("Until that endpoint ships") and the TLS-discovery-failure
  breadcrumb are corrected; the latter now points operators at
  `--client-id`/`--issuer` overrides **and** root-CA installation for
  internal-CA deployments. Deployer recipe for the pre-created public
  `meho-cli` Keycloak client added to
  [`deploy/values-examples/README.md`](deploy/values-examples/README.md).
  v0.3.1 first-login regression on the documented happy path; consumer
  report 2026-05-21 Signal #16 ([#789](https://github.com/evoila/meho/issues/789),
  G0.9.1-T9 under [#772](https://github.com/evoila/meho/issues/772)).
  Auto-provisioning the public client at install time is tracked under
  [#791](https://github.com/evoila/meho/issues/791) (T11).
- Backfill curated per-group `when_to_use` text onto existing
  `operation_group` rows on upgrade (Alembic `0011`), closing the
  Signal #5 gap where #731/#732's curation never replaced the
  v0.3.0-era auto-derived templates already written to the DB. The
  migration rewrites only rows still holding the template prefix —
  operator edits via `meho.connector.edit_group` and tenant-scoped rows
  are preserved — and is idempotent; Harbor's placeholder group text is
  curated in the same pass. Signal #5 (refined: backfill-on-upgrade,
  not curate-existing) ([#774](https://github.com/evoila/meho/issues/774)
  / #783).
- MCP `add_to_memory` now injects the default user-scope TTL when `ttl`
  is omitted, matching the REST path — a shared resolver distinguishes
  "omitted" (apply `MEMORY_USER_DEFAULT_TTL_DAYS`) from explicit
  `ttl: null` (the `--persist` opt-out, persist forever). The v0.3.1 MCP
  path silently bypassed the default and stored `expires_at = null`.
  Signal #10 ([#775](https://github.com/evoila/meho/issues/775) / #781).
- The token validator returns a specific code at the decode stage —
  `invalid_audience` / `invalid_issuer` / `missing_sub` /
  `token_expired` / `signature_verification_failed` / … — instead of a
  bare `invalid_token`, so a deployer sees which claim failed. Per
  RFC 6750 the response body stays terse and the full
  expected-vs-received diagnostic goes to the structured log. Addendum II
  Ask #1 (Walls #2/#3) ([#797](https://github.com/evoila/meho/issues/797)
  / #842).
- `meho login`'s device-code poll no longer dies with `context deadline
  exceeded` under wrapped / non-interactive invocation (CI, an agent's
  shell tool): the device-flow wait is detached from the ambient parent
  context and bounded by its own deadline matching Keycloak's device-code
  TTL. Addendum II Wall #4
  ([#798](https://github.com/evoila/meho/issues/798) / #821).
- `meho login` on macOS now falls back to the `0600` credentials file
  store when the system keyring rejects the token bundle with a size
  error (`go-keyring` hitting the legacy `kSecValueData` ~4 KB limit),
  instead of failing the whole login; `MEHO_KEYRING_DISABLE` is now
  surfaced in `--help` to force the file store. Addendum II Wall #5
  ([#876](https://github.com/evoila/meho/issues/876)).

### Documentation

- **Target-reference shape convention documented** for the MCP
  agent surface. The agent surface today carries three internally
  coherent but cross-tool-divergent shapes for naming a target /
  node — `call_operation` takes `target: {name: ...}` (object),
  `query_topology` / `query_audit` take `target: "<name>"` (bare
  string), `meho.topology.annotate` takes paired `from_name` /
  `to_name`. The 2026-05-21 RDC second-cycle dogfood (Signal #8)
  flagged this as migration fatigue across tools. A new
  "Target-reference shape convention" section in
  [`docs/architecture/mcp.md`](docs/architecture/mcp.md) captures
  the three shapes, the rationale for each, and the forward
  convention any new tool should follow — so no fourth shape lands
  by accident before the deliberate v0.4+ unification. The
  `call_operation` / `query_topology` / `query_audit` tool
  descriptions now cross-reference this section. **No wire-schema
  change** — this is docs-only ([#780](https://github.com/evoila/meho/issues/780)).
- **kb slug leading-letter constraint surfaced in schema descriptions**
  for the `add_to_knowledge` MCP tool and the `POST /api/v1/kb`
  request body. The slug regex requires a leading lowercase letter,
  but the existing example (`vcenter-9.0-snapshot-revert`)
  satisfied the rule silently — a caller running a digit-leading
  slug (`657-recovery`) tripped a -32602 / 422 without ever seeing
  the constraint in the schema. Both descriptions now name the
  rule and pair the positive example with a digit-leading negative
  example, so the constraint is visible before the call goes out
  ([#780](https://github.com/evoila/meho/issues/780), Signal #15).
- Publish a consolidated **deployer auth-onramp recipe** (5-step
  realm walk + 4-wall symptom→cause→fix matrix) covering both the
  `meho login` CLI device-code path and the MCP-client onramp.
  Lives in [`deploy/values-examples/README.md` § Auth onramp
  recipe (CLI + MCP)](deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp);
  cross-linked from `docs/cross-repo/mcp-client-setup.md` (the
  pre-registered-public-client requirement is now surfaced up
  front, not buried at Step 2) and `docs/acceptance/install.md`.
  Closes the ~2.5-hour first-login wall the 2026-05-21 RDC
  dogfood walked (Addendum II Ask #3), including the
  `basic`/`sub` Keycloak 25+ gotcha (admin-API-created clients
  don't auto-inherit realm default-default scopes, so `sub` is
  missing and tokens are rejected with opaque `invalid_token`)
  and the `.mcp.json` `client_id` limitation for Claude Code +
  Cursor (RFC 7591 DCR is closed by Keycloak's Trusted Hosts
  policy on any prod realm; the deployer-side fix doesn't help
  until those clients expose `client_id` — shim through
  `mcp-remote` is the workaround). Docs-only; no backplane code
  change (the RFC 9728 surface is correct). G0.9.1-T10 under
  [#772](https://github.com/evoila/meho/issues/772) /
  [#790](https://github.com/evoila/meho/issues/790).
  Auto-provisioning the recipe at install time is tracked under
  [#791](https://github.com/evoila/meho/issues/791) (T11);
  token-validator error specificity is
  [#797](https://github.com/evoila/meho/issues/797) (T12); the
  `meho login` device-flow deadline fix is
  [#798](https://github.com/evoila/meho/issues/798) (T13).

### Performance (internal — CI / test-suite, no operator-facing change)

- Unit-job CI time brought back under budget as the G3.6 op count grew:
  skip the per-test typed-descriptor re-embed in the unit suite
  ([#771](https://github.com/evoila/meho/issues/771) / #799),
  session-scope the fastembed model cache dir to kill per-test model
  re-fetch (#786), amortize per-test DB schema via a per-worker template
  (#898), run `python-lint-test` on `meho-runners-ci-heavy` at `-n 6`
  ([#761](https://github.com/evoila/meho/issues/761) / #765), and restore
  `--cov` on PRs while lowering the job timeout 50→20 min (#814). An
  opt-in real-embedding guard + CI-perf timing instrumentation (#827)
  backs the measurement.
- Test correctness: the G8.1 audit acceptance test asserts `422` for a
  body-level `tenant_id` (the `extra="forbid"` contract) (#767).

## [0.3.1] - 2026-05-21

**v0.3.0 dogfood-hardening patch.** No new headline features — this
release closes the eight signals + two ingest sharp-edges surfaced by
the 2026-05-20 RDC operator-team in-lab dogfood against the freshly
tagged v0.3.0. Initiative [G0.9 #737](https://github.com/evoila/meho/issues/737)
parents the ten Tasks; this section follows the three-state release-
notes convention codified by T7 (per
[`docs/codebase/connector-release-readiness.md`](docs/codebase/connector-release-readiness.md)).

> **What v0.3.1 ships:** correctness, observability, and release-
> notes-vocabulary tightenings on top of v0.3.0's dispatch + catalog
> surface. Every typed connector's `operation_count` now matches its
> `group_count` for the universe of rows actually advertised;
> `register_connector_v2`-only connectors (harbor, sddc-manager) are
> visible in `GET /api/v1/connectors` instead of invisible-until-ops-
> register; uvicorn honours `X-Forwarded-Proto` from a trusted
> Ingress so the trailing-slash 307 redirects survive TLS
> termination; every public v1 request schema is `extra="forbid"`
> so v0.2.1 clients sending old field names get a fail-loud 422
> instead of silent-drop; per-group `when_to_use` strings are
> curated and the kwarg is now required so future connectors can't
> regress to template literals; the spec-ingestion pipeline
> validates the operator-supplied `version` label against both
> `spec.info.version` AND the registered connector classes'
> `supported_version_range` at ingest time, surfacing
> orphaned-ops-at-ingest instead of `NoMatchingConnector` at
> first dispatch.
>
> **What v0.3.1 does NOT change:** the `NotImplementedError` stubs
> for the per-target-credential connectors' loaders
> (`load_kubeconfig_from_vault` / `load_session_credentials_from_vault`)
> remain in tree, tracked under the open
> [Goal #214 (Connector parity)](https://github.com/evoila/meho/issues/214).
> Adopters running `operations/call k8s.namespace.list target=...`
> against a real Vault-backed target still receive
> `NotImplementedError` — see the v0.3.0 callout above for the full
> three-state rubric. v0.3.1 makes the surrounding release-notes
> vocabulary honest (Goal #214 body reframed by T6 to spell out the
> dual-layer model — composites + generic-ingested raw REST — so
> adopters can plan layer-2 ingest as their long-tail coverage
> path).

### Breaking changes

- **`POST /api/v1/retrieve`, `POST /api/v1/operations/call`, and
  every other public v1 request body** now reject unknown fields
  with HTTP 422 `extra_forbidden`
  ([#729](https://github.com/evoila/meho/issues/729) /
  [#746](https://github.com/evoila/meho/pull/746)). v0.2.1 clients
  that still send the pre-v0.3.0 names (`q` / `top_k` on
  `/retrieve`, bare-string `target` on `/operations/call`) used to
  silently fall back to defaults or empty; they now fail-loud. This
  is the load-bearing half of the v0.3.0 schema renames the
  [0.3.0] section's `Breaking changes` already enumerates —
  migrations there are unchanged; v0.3.1 just removes the silent-
  drop escape hatch.

  Migration: send the canonical field names already documented in
  the [0.3.0] breaking-changes recipes. If you maintain a v0.2.1-
  compatible client, gate your encoder on the deployed backplane
  version and switch on the v0.3.0 schema for any
  v0.3.0-or-later target.

- **`register_typed_operation` + `register_composite_operation`
  signatures** now require `when_to_use` as a keyword-only
  argument ([#731](https://github.com/evoila/meho/issues/731) /
  [#757](https://github.com/evoila/meho/pull/757)). The auto-
  derived `"Operations grouped under {group_key!r} for {product}
  {impl_id}."` default is removed; out-of-tree connector authors
  must supply an explicit agent-actionable string per group.
  Empty / whitespace-only strings are normalised to `None` when
  `group_key is None`. Internal API — affects any third-party
  connector registering ops against MEHO's typed-op registry.

  Migration: pass `when_to_use="<one-line agent-actionable
  selection signal>"` to every `register_typed_operation(...)` /
  `register_composite_operation(...)` call. See the curated
  strings the v0.3.1 in-tree connectors ship for shape examples
  ([#732 / #756](https://github.com/evoila/meho/pull/756)).

### Added

- **Curated per-group `when_to_use` strings** for every shipped
  typed connector — kubernetes (7 groups), vault (3 groups), bind9
  (4 groups), vmware-rest composites (7 groups)
  ([#732](https://github.com/evoila/meho/issues/732) /
  [#756](https://github.com/evoila/meho/pull/756)). Replaces the
  v0.3.0 template-literal placeholders so an LLM consuming the
  catalog gets a real selection signal between sibling groups
  (`vault.kv` vs `vault.sys` vs `vault.auth`, etc.).
- **Ingest-time `spec.info.version` ↔ operator-label validation**
  ([#740](https://github.com/evoila/meho/issues/740) /
  [#762](https://github.com/evoila/meho/pull/762)). `POST
  /api/v1/connectors/ingest` now classifies the operator-supplied
  `version` against each spec's `info.version` as `exact` /
  `compatible` / `incompatible`. Incompatible labels (e.g. ingesting
  vCenter-9 spec under `version="8.0"`) return 422 with both
  versions in the detail; compatible-drift emits a structured
  `connector_ingest_version_drift` event and proceeds.
- **Ingest-time class-coverage pre-flight**
  ([#741](https://github.com/evoila/meho/issues/741) /
  [#763](https://github.com/evoila/meho/pull/763)). `POST
  /api/v1/connectors/ingest` now checks that the
  `(product, version, impl_id)` triple is in at least one registered
  connector class's `supported_version_range` BEFORE the
  `endpoint_descriptor` row creation. Outside-of-range with a class
  present → 422 with the class's advertised range; no class
  registered for `(product, impl_id)` yet → warn-but-proceed via a
  `connector_ingest_orphaned_class` structured event (the v0.4-
  staging path where ops land before the class exists).
- **Connector release-notes convention** codified in CHANGELOG.md
  + cross-referenced from
  [`docs/codebase/connector-release-readiness.md`](docs/codebase/connector-release-readiness.md)
  ([#735](https://github.com/evoila/meho/issues/735) /
  [#759](https://github.com/evoila/meho/pull/759)). Three states —
  *dispatch + catalog landed*, *loader wired (single auth model)*,
  *ops curated for production* — every connector release line now
  says which state the release ships, not the next state up.

### Changed

- **`/api/v1/connectors` lists `register_connector_v2`-only
  entries** with `group_count: 0, operation_count: 0` instead of
  hiding them until ops register
  ([#733](https://github.com/evoila/meho/issues/733) /
  [#758](https://github.com/evoila/meho/pull/758)). Operators see
  "Harbor / sddc-manager registered, no ops yet" as a first-class
  list row, matching the natural expectation that *connector
  registered ⇒ visible in list*.
- **Goal #214 (Connector parity) body reframed** to spell out the
  dual-layer architecture — Layer 1 (hand-coded composites) +
  Layer 2 (generic-ingested raw REST via the G0.7 ingest pipeline)
  — so adopters can plan layer-2 ingest as the long-tail coverage
  path instead of waiting for a 1:1 binding that was never the
  plan ([#734](https://github.com/evoila/meho/issues/734) /
  [#760](https://github.com/evoila/meho/pull/760)). Companion
  artifact: `docs/cross-repo/goal-214-reframe-2026-05-20.md`.

### Fixed

- **`/api/v1/connectors` `operation_count` rollup now counts
  typed + composite + ingested rows uniformly**
  ([#728](https://github.com/evoila/meho/issues/728) /
  [#747](https://github.com/evoila/meho/pull/747)). v0.3.0
  rolled up `operation_count: 0` for every typed connector
  (`bind9-ssh-9.x`, `k8s-1.x`, `vault-1.x`, `vmware-rest-9.0`)
  because `_operation_count_by_connector` carried a stale
  `source_kind == "ingested"` filter while the paired groups
  aggregator counted all source-kinds. Operators (and LLMs)
  reading the list could conclude the catalog was empty for every
  typed connector and move on. The two paired queries now count
  the same universe of rows.
- **uvicorn `--proxy-headers` + chart `FORWARDED_ALLOW_IPS`**
  ([#730](https://github.com/evoila/meho/issues/730) /
  [#748](https://github.com/evoila/meho/pull/748)). The backplane
  behind a TLS-terminating Ingress used to emit trailing-slash
  307 `Location` headers with a bare `http://` scheme — security-
  adjacent (an active interceptor could MITM the second hop). The
  Dockerfile CMD adds `--proxy-headers`; the chart exposes
  `config.forwardedAllowIps` (rendered into the
  `FORWARDED_ALLOW_IPS` env var uvicorn reads natively). Default
  `127.0.0.1` matches uvicorn's secure default and fails-closed
  in-cluster — operators MUST override with their Ingress
  controller's pod CIDR (e.g. `10.42.0.0/16` for RKE2 default)
  per the new `docs/cross-repo/reverse-proxy-contract.md`
  runbook.

## [0.3.0] - 2026-05-20

**MVP2 — kubernetes + vault + bind9 + topology.** Five Initiatives
closed (G3.2 / G3.3 / G3.4 / G9.1 / G9.2). Three structural backstops
landed against the green-but-hollow class of failure that surfaced
during the closure push: dispatcher MRO-aware binding, registration-
time `handler_ref` resolvability guard, and the `Python (integration
testcontainers)` lane is now a required merge gate.

> **What v0.3.0 ships for the new connectors (k8s / bind9-ssh / vault / vmware-rest):**
> dispatch + catalog + per-op metadata + safety annotations + `search_operations` indexing
> + integration-test coverage (against injected loaders for k8s + vmware-rest, against
> real Vault for the existing `vault-1.x` connector). The bind9-ssh connector executes
> end-to-end against a real bind9 SSH target.
>
> **What v0.3.0 does NOT ship for the per-target-credential connectors (k8s + vmware-rest):**
> the loader that reads operator-context per-target Vault credentials. Both
> `load_kubeconfig_from_vault` and `load_session_credentials_from_vault` remain
> `NotImplementedError` stubs in production, tracked under the open
> [Goal #214 (Connector parity)](https://github.com/evoila/meho/issues/214).
>
> Adopters running a v0.3.0 deploy with `operations/call k8s.namespace.list target=...`
> against a real Vault-backed target will receive `NotImplementedError` — not
> "the connector works." The catalog is real and indexed; production execution
> needs Goal #214 to land per-connector. See
> [`docs/codebase/connector-release-readiness.md`](docs/codebase/connector-release-readiness.md)
> for the three-state rubric (dispatch + catalog / loader wired / ops curated).

### Breaking changes

Amended 2026-05-20 ([#735](https://github.com/evoila/meho/issues/735)) after the
RDC operator-team dogfood surfaced two v0.2.1 → v0.3.0 schema changes
that shipped without CHANGELOG coverage. Both affect adopters who
authored v0.2.1 client code against the public REST surface.

- **`POST /api/v1/operations/call` — `target` field shape.** Changed
  from bare string to object descriptor. A v0.2.1 client encoding
  `target: "rdc-vault"` now gets HTTP 422 (`dict_type`) on first call
  after upgrade.

  Migration (one-character change per call site):

  ```diff
  - {"op_id": "vault.kv.read", "target": "rdc-vault", "params": {...}}
  + {"op_id": "vault.kv.read", "target": {"name": "rdc-vault"}, "params": {...}}
  ```

  The new shape accepts the full target descriptor — `name`, `id`, or
  fingerprint-match — via the G0.3 target-resolver. The old bare-string
  shape is not aliased; aliasing was considered and rejected (see
  [#729 (T2)](https://github.com/evoila/meho/issues/729) which tightens
  `extra="forbid"` across all v1 schemas — extending an alias would
  cut against that direction).

- **`POST /api/v1/retrieve` — field renames.** `q` → `query`;
  `top_k` → `limit`. A v0.2.1 client sending the old names will receive
  HTTP 422 once [#729 (T2 — `extra="forbid"`)](https://github.com/evoila/meho/issues/729)
  lands; until then the old names silently fall back to defaults
  (`query=""`, `limit=10`) and the retrieve call returns unrelated results.

  Migration:

  ```diff
  - {"q": "vault rotation", "top_k": 20}
  + {"query": "vault rotation", "limit": 20}
  ```

  `query` aligns the retrieve surface with the agent-facing
  `search_operations(connector_id, query)` vocabulary already used
  through MCP; `limit` is the Keep-a-REST convention for pagination
  size and aligns with the `list_operations` / `list_targets` surfaces.

### Added

- **G3.2 — Kubernetes typed connector** (#320). 13 ops via
  `kubernetes_asyncio` against G0.6's typed-op registry. Ops:
  `k8s.ls`, `k8s.namespace.list/info`, `k8s.node.list`,
  `k8s.pod.list/info`, `k8s.deployment.list/info`,
  `k8s.service.list`, `k8s.ingress.list`,
  `k8s.configmap.list/info`, `k8s.events.list`, `k8s.logs`.
  Kubeconfig is fetched from Vault by `secret_ref`; k3d-backed CI
  acceptance suite. CLI: `meho k8s …`. Replaces the consumer's
  `kubectl-vcf.sh` wrapper. Onboarding: see [`docs/cross-repo/k8s-onboarding.md`](docs/cross-repo/k8s-onboarding.md).
- **G3.3 — Vault typed op surface** (#366). KV-v2 + sys + auth
  read/list ops registered via `register_typed_operation()`. Ops:
  `vault.kv.list/put/versions/delete`, sys read group, auth read
  group (userpass + approle). G6 credential_read classifier
  exerciser. CLI: `meho vault kv/sys/auth …`. Dev-mode CI
  integration harness. Onboarding: [`docs/cross-repo/vault-onboarding.md`](docs/cross-repo/vault-onboarding.md).
- **G3.4 — bind9 typed-SSH connector** (#367). First
  `SshConnector` tier-1 child against the G0.2 Connector ABC. 11
  ops: `bind9.about`, `zone.list/read`, `record.get/add/remove`,
  `config.show/apply_file/apply_views/backup/reload`. Atomic-apply
  discipline — every write op rolls back on `named-checkconf` or
  dig-verify failure, leaving `/etc/bind/` exactly as it was
  pre-op. Replaces the consumer's `bind9-dns.sh` wrapper (the
  heaviest in the inventory). CLI: `meho bind9 …`. Onboarding +
  credential-leak postmortem links: [`docs/cross-repo/bind9-onboarding.md`](docs/cross-repo/bind9-onboarding.md).
- **G9.1 — Topology graph substrate + auto-discovery** (#363).
  `graph_node` + `graph_edge` tables (Alembic 0007). Closed v0.2
  14-kind node vocabulary + 4-kind auto-discoverable edge
  vocabulary. `Connector.discover_topology` hook on the connector
  ABC. Recursive-CTE query verbs (`dependents` / `dependencies` /
  `path`) with cycle detection. Background refresh service.
  REST + CLI + MCP surfaces; tenant-scoped throughout. CLI:
  `meho topology refresh/dependents/dependencies/path` and
  `meho targets discover`. MCP: `query_topology` + `list_targets`
  meta-tools. Implements ~70% of [decision #6](docs/planning/v0.2-decisions.md)'s
  auto-discoverable half.
- **G9.2 — Curated cross-system edges + annotation flow** (#364).
  Closed v0.2 10-kind edge vocabulary (Alembic 0010) extends the
  auto-discoverable four with six operator-curated kinds. CLI:
  `meho topology annotate/unannotate/list-edges`. Same-kind /
  incompatible-kind conflict resolution with bidirectional
  `properties.conflicts_with` markers; supersede-on-curate;
  refresh sticky-supersede. Tenant-boundary + 10k-node
  performance acceptance. Implements the ~30% operator-curated
  half of [decision #6](docs/planning/v0.2-decisions.md).

### Security

- **`_remote_bash_with_sudo()` line-1/line-2/line-3+ stdin
  discipline** (#703, #707). Closes the 2026-05-04 / 2026-05-05
  bind9 credential-leak surface. The primitive uses `head -c
  <byte-count>` to slice the script off stdin before `sudo -S`
  reads the trailing password line, so sudo cannot swallow
  script bytes (the original mis-ordered-stdin made six bind9
  write ops silently no-op in production). A repo-tree grep
  guard ([`test_remote_bash_with_sudo_is_only_sudo_construction_in_connectors_tree`](backend/tests/integration/test_g3_4_bind9_e2e.py))
  asserts no other sudo construction can exist anywhere under
  `connectors/`.

### Changed

- **`Python (integration testcontainers)` is a required merge
  gate** (#698). Promoted from advisory to required after the
  bind9 G3.4 Initiative closed green-but-hollow once with this
  lane's per-op `call_operation` integration tests red. Any
  future regression of agent-facing dispatch (any connector, any
  op) now blocks merge instead of closing an Initiative green.
- **`graph_node.kind` closed-vocabulary discipline tightened**
  (#712). The migration's `ck_graph_node_kind` CHECK constraint
  + `_GRAPH_NODE_KINDS` ORM constant + every test fixture must
  agree on the same closed v0.2 14-kind set. Widening is a
  coordinated DB + model migration, not a test-only change.
- **Backplane image bakes the fastembed default model** (#577).
  Fixes the v0.2 cold-start hang that needed network access on
  first boot.

### Fixed

- **`handler_unreachable` dispatcher fix** (#697 / #699 / #713).
  Three layers:
  - #699: [`is_unbound_method`](backend/src/meho_backplane/operations/_handler_resolve.py)
    is now MRO-aware identity-matching, not a
    `__qualname__.startswith(cls.__name__)` heuristic that missed
    subclass + mixin cases (which had silently no-op'd the bind9
    `about` op through `call_operation`).
  - #699 (paired): the typed-dispatch branch now fails loud on a
    handler that still has `self` as its first param, instead of
    silently dropping it and crashing with a confusing
    `TypeError` further downstream.
  - #713: [`register_typed_operation`](backend/src/meho_backplane/operations/typed_register.py)
    + `register_composite_operation` call the dispatcher's
    `import_handler` immediately after `derive_handler_ref`
    returns, re-raising as `HandlerRefError` with `op_id` /
    `product` / `version` / `impl_id` context. A connector cannot
    ship green with an unreachable handler_ref anymore —
    registration fails at FastAPI lifespan start.
- **Dispatcher: `audit_*` contextvars not surfacing on the audit
  row** (#704). The dispatcher's `_build_audit_payload` now reads
  every `audit_*` contextvar bound by a handler (mirrors the
  FastAPI middleware's [`_resolve_audit_payload()`](backend/src/meho_backplane/audit.py)
  pattern). Bind9 write ops carry `state_before` / `state_after`
  on the `audit_log` row.
- **MCP audit-row writer: `audit_*` contextvars not surfacing**
  (#720). The parallel of #704 one architecture-layer over —
  [`write_mcp_audit_row`](backend/src/meho_backplane/mcp/audit.py)
  now merges `_resolve_audit_payload()` into the row payload.
  Caller-supplied keys win on collision so MCP envelope identity
  fields (`op_id` / `op_class` / `params_hash`) stay
  authoritative.
- **CI: process-wide registry isolation under `pytest-xdist`**
  (#585 / #603 / #604). The unit lane drops from ~49 min to
  ~6 min after enabling `pytest -n auto`.
- **Bind9 e2e `_restore_etc_bind` fixture stdin discipline**
  (#702). The CI fixture's `sudo -S -p ''` plus a leading `\n`
  write was corrupting the snapshot-restore tar stream; the e2e
  suite now drives the restore through the same load-bearing
  primitive as production.

### Notable PRs in this release

[#320](https://github.com/evoila/meho/pull/320) /
[#366](https://github.com/evoila/meho/pull/366) /
[#367](https://github.com/evoila/meho/pull/367) /
[#363](https://github.com/evoila/meho/pull/363) /
[#364](https://github.com/evoila/meho/pull/364) — the five
Initiatives — plus the green-but-hollow chain:
[#591](https://github.com/evoila/meho/pull/591) →
[#697](https://github.com/evoila/meho/pull/697) →
[#699](https://github.com/evoila/meho/pull/699) →
[#702](https://github.com/evoila/meho/pull/702) →
[#703](https://github.com/evoila/meho/pull/703) →
[#704](https://github.com/evoila/meho/pull/704) →
[#698](https://github.com/evoila/meho/pull/698) →
[#713](https://github.com/evoila/meho/pull/713) →
[#720](https://github.com/evoila/meho/pull/720).

## [0.2.0] - 2026-05-16

**MVP1 — substrate + vSphere + KB.** The v0.2.0 release body lived in
`[Unreleased]` at tag time; the section below preserves what shipped.

### Added

- **Backplane image:** multi-arch (`linux/amd64` + `linux/arm64`)
  container image at `ghcr.io/evoila/meho`, built and pushed by
  `.github/workflows/image.yml` on every push to `main` and on
  `v*` tag pushes. Cosign keyless-signed per ADR 0006 — operators
  verify with `cosign verify ghcr.io/evoila/meho:<tag>` using the
  identity-claim regex anchored on `image.yml`. The `:latest` tag
  is deliberately never published; operators pin to
  `sha-<git-sha>` or `v<x.y.z>`. (#34)
- **Helm chart:** the deploy contract at `deploy/charts/meho/`,
  published as an OCI artefact at `oci://ghcr.io/evoila/meho-chart`
  by `.github/workflows/chart.yml`. Cosign keyless-signed on every
  push; anonymous-pull verified by the publish workflow before the
  job exits green. Calver-bumped on `main`
  (`0.1.YYYYMMDD-<short-sha>`); plain semver on `v*` tag pushes.
  (#41)
- **Typed values contract:** `deploy/charts/meho/values.schema.json`
  (JSON Schema draft-07). Rejects empty operator-required fields
  (`image.tag`, `vault.address`, `keycloak.issuer`,
  `postgres.credentialsSecret`, NetworkPolicy CIDRs when enabled,
  Ingress host + TLS secret when enabled), pattern-validates IPv4
  CIDRs + hostnames + OCI image refs, and rejects unknown keys at
  every object level (`additional properties '<name>' not allowed`).
  Misconfigured installs fail at `helm install` / `helm upgrade` /
  `helm template`, not at first request. (#38)
- **Sanitized example values:**
  [`deploy/values-examples/values-rdc-example.yaml`](./deploy/values-examples/values-rdc-example.yaml)
  templates the supported Vault + Keycloak + Postgres deploy shape
  (the RDC Hetzner lab shape). All site-specific fields use
  `<REPLACE: ...>` placeholders that fail the schema at install
  time, so an operator who forgets to substitute one fails-loud at
  `helm install`. ESO sync patterns documented in the companion
  README. (#40)
- **kind-local values overlay:**
  [`deploy/values-examples/values-kind.yaml`](./deploy/values-examples/values-kind.yaml)
  for a 5-minute laptop deploy that exercises the chart's install
  plumbing (pre-install migration Job, Deployment, broadcast
  subchart). Only Postgres ships a real in-cluster mock manifest
  (Namespace + Secret + Deployment + Service for `postgres:16-alpine`,
  documented at the top of the overlay); Vault and Keycloak are
  *placeholder URIs* so the chart's URI-validated fields resolve at
  install time — no in-cluster Vault or Keycloak is deployed and no
  real auth flow runs. Operator identity is faked; federation probes
  register but `meho login` will not complete end-to-end. For real
  federation use the existing-k8s flow. (#60)
- **Multi-platform CLI release pipeline:** `linux/amd64`,
  `linux/arm64`, `darwin/amd64`, `darwin/arm64` tarballs published
  to GitHub Releases on every `v*` tag push, with a combined
  `SHA256SUMS` file. Driven by GoReleaser via
  `.github/workflows/cli-release.yml`. (#46 / #178)
- **Cosign keyless signing of every CLI release artefact** (four
  tarballs + `SHA256SUMS`) per ADR 0006. Each artefact ships with
  a matching `.cosign.bundle` sigstore bundle (signature + Fulcio
  cert + Rekor proof, single JSON file). Verification recipe
  documented at the top-level README and `cli/README.md`. (#47)
- **OSS day-1 documentation:** top-level `README.md` now ships a
  hero + "Deploy → Local (kind)" + "Deploy → Existing k8s" +
  "Verify image + chart + CLI signatures" + architecture overview
  + chart values reference. `CONTRIBUTING.md` expanded with the
  dogfood-loop framing, public-from-day-1 norm, bidirectional
  coordination flow, and DCO sign-off discipline. This CHANGELOG
  reframed as project-wide (image + chart + CLI under one
  document). (#60)
- **Cold-deploy acceptance contract:** producer-side specification
  of Goal #11 DoD bullet 1 (`install.sh` cold-deploy → working
  MEHO at meho.evba.lab in <5 min) lives at
  [`docs/acceptance/install.md`](./docs/acceptance/install.md).
  Companion verifier
  [`scripts/acceptance/install-verify.sh`](./scripts/acceptance/install-verify.sh)
  is invoked as the last step of the consumer's `install.sh` on
  `claude-rdc-hetzner-dc`; its exit code is the cold-deploy's exit
  code. Asserts deployment Ready, migration Job succeeded,
  `/healthz` 200, `/version` reports the deployed git SHA,
  `/api/v1/health` unauthenticated returns 401, audit middleware
  is reachable, and wall-clock budget ≤ 300s (warn by default,
  hard-fail with `--enforce-budget`). Optional authenticated
  probes when `MEHO_ACCESS_TOKEN` is set. (#55)
- **Helm-rollback acceptance contract:** producer-side specification
  of Goal #11 DoD bullet 3 (`helm rollback meho` verified
  end-to-end with a non-trivial schema diff) lives at
  [`docs/acceptance/rollback.md`](./docs/acceptance/rollback.md).
  Companion verifier
  [`scripts/acceptance/rollback-verify.sh`](./scripts/acceptance/rollback-verify.sh)
  asserts the cluster-level forward-compat property: after a
  `helm upgrade` to N+1 with a non-trivial additive migration and
  a `helm rollback` back to N, the running Pod is the N image, the
  schema retains the N+1 columns (no down-migration ran), and the
  public surface (`/healthz`, `/version`, `/api/v1/health`) serves
  traffic correctly. Sample synthetic migration at
  [`scripts/acceptance/synthetic-n-plus-1.sql`](./scripts/acceptance/synthetic-n-plus-1.sql)
  lets the exercise reuse a documented N→N+1 change without
  authoring a one-shot alembic migration. Complements the
  unit-level forward-compat regression test at
  [`backend/tests/test_migration_rollback.py`](./backend/tests/test_migration_rollback.py)
  (Task #30) — two layers of forward-compat assurance. (#57)
- **Green-smoke counter + `targets.yaml` rdc-meho schema:**
  producer-side specification of Goal #11 DoD bullets 4 and 5.
  [`docs/acceptance/green-counter.md`](./docs/acceptance/green-counter.md)
  codifies the 5-consecutive-merged-PR green-smoke counter — scope,
  exclusions, data source (`pr-smoke.yml` workflow-run history),
  reference algorithm, and three read surfaces (Shields badge,
  one-shot CLI, chassis probe).
  [`docs/cross-repo/targets-yaml.md`](./docs/cross-repo/targets-yaml.md)
  ships the cross-repo schema for the consumer's `targets.yaml`
  `rdc-meho` entry — required + recommended fields, a worked
  example, anti-patterns, and the chassis health-probe contract
  (authenticated `/api/v1/health` + anonymous `/healthz`
  fallback). The
  [README badge](./README.md)
  carries a placeholder the maintainer swaps for a live Shields
  endpoint URL once the consumer-side counter is up.
  Counter implementation and the `targets.yaml` entry land on
  `claude-rdc-hetzner-dc` per the producer/consumer split (draft
  consumer issue body at
  [`docs/cross-repo/issue-58-consumer-ticket-body.md`](./docs/cross-repo/issue-58-consumer-ticket-body.md)).
  (#58)

### Changed

- **CHANGELOG scope is project-wide.** Previously this file was
  CLI-only scaffolding for `--release-notes` extraction; it now
  records every operator-facing change across image, chart, and
  CLI. The `cli/CHANGELOG.md` scaffold is superseded — this is the
  single source of truth. (#60)
- GitHub Release body is now sourced from this CHANGELOG via
  `--release-notes` rather than GoReleaser's auto-generated
  git-log. The workflow extracts the section matching the current
  tag (or `[Unreleased]` as fallback). (#47)

## [0.1.0-beta] - planned TBD

Initial v0.1-beta release: backplane chassis, federation probes,
audit, container image, Helm chart, operator CLI, CI/CD with per-PR
ephemeral cluster smoke. The v0.1-beta surface is intentionally
narrow per Goal #11: enough for an operator to install MEHO into a
Kubernetes cluster, log in, and verify the federation chain is
healthy. Operations (cluster inventory, policy enforcement, audit
queries, etc.) land in v0.2+ through the CLI's server-driven
discovery mechanism — adding an operation does not require a new
CLI release.

`v0.1.0` (non-beta) ships when Goal #59 (first connector + wrapper
replacement) closes — the beta tag exists to distinguish the
chassis-only milestone from the first user-visible operation.

The v0.1 trust chain across all three operator-facing artefacts —
the backplane container image, the Helm chart, and the CLI release
tarballs — is built on cosign keyless signing under a common
identity-claim format (ADR 0006). Operators verify each artefact
against the workflow path that produced it using
`cosign verify` / `cosign verify-blob` with
`--certificate-identity-regexp` — no public-key distribution, no
key custody.

See [Goal #11](https://github.com/evoila-bosnia/meho-internal/issues/11)
for the full v0.1-beta scope.
