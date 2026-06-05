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

### CLI — `targets discover` points at the real registration verb (#1536)

- Repoint `meho targets discover` (help text, post-run output, and the
  command doc-comment) from the nonexistent `meho targets create` to
  `meho targets import`, the verb that actually registers a reviewed
  candidate. The stale `(auto-registration is v0.2.next)` aside is
  reworded so it no longer dangles on a verb that does not exist.

### Connector ingest — hand-authored spec on-ramp (#1533)

- Document the hand-authored-OpenAPI-3.x → `--spec file://…` route as the
  intended on-ramp for products that publish **no** OpenAPI spec (VCF
  Fleet / vRSLCM, Hetzner Robot): a "Product publishes no OpenAPI spec"
  section in `docs/cross-repo/connector-ingestion.md` with a minimal
  worked example, and the catalog-miss `next_step` rationale widened to
  name it so a 0-op `state=registered` connector no longer reads as a
  dead end.

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
  first consumer.
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

### Fixed

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
