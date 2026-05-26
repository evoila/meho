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
  `claude-rdc-hetzner-dc#697`. (#1142)
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
  `claude-rdc-hetzner-dc#697`. (#1146)

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
contract executable, and the **G0.6.1** JsonFluxReducer wiring. No
breaking changes.

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
  `GET /api/v1/audit/replay` with a 10k count-first 413 cap
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
  Pydantic schemas + 6 tenant-scoped + RBAC-gated API routes
  (list / show / create / update / delete / history;
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
  timeline.** New `meho topology history` + `GET /api/v1/topology/history`
  + `query_topology(kind=history)` expose per-node/edge mutation history
  ([#936](https://github.com/evoila/meho/issues/936)); `meho topology
  diff <ts1> <ts2>` + `GET /api/v1/topology/diff` +
  `query_topology(kind="diff", ts1=..., ts2=...)` returns the net change
  set folded to `created` / `updated` / `removed` with a 1000-row cap
  bounded at the SQL layer
  ([#931](https://github.com/evoila/meho/issues/931), follow-up SQL
  bound #987 / #1000). Cross-Initiative integration suite covers the
  full history surface ([#1027](https://github.com/evoila/meho/issues/1027)).

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
