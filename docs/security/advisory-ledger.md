# Security advisory ledger

The reconciliation behind v1.0 gate 2 — *"pending security advisories
published per `SECURITY.md`"* (Goal #2661; Task #3380). It maps every
`### Security` entry in [`CHANGELOG.md`](../../CHANGELOG.md) to a class
and a disposition, so the gate is a table lookup rather than an
opinion. `SECURITY.md` names GitHub repository security advisories
(GHSA) as the publication vehicle; this file is the ledger that the
rc-series release step reconciles against.

## Snapshot

| Measurement | Value | How measured |
|---|---|---|
| Date | 2026-09-22 | — |
| CHANGELOG revision | `64d1ad28` (`main`, v0.35.12 + `[Unreleased]`) | `git log -1 --oneline` |
| `### Security` headings | **77** | `grep -cE "^### Security" CHANGELOG.md` |
| Ledger rows below | **77** | must equal the heading count |
| Published GHSAs on `evoila/meho` | **0** | `gh api /repos/evoila/meho/security-advisories --jq 'length'` |
| Externally reported vulnerabilities cited in CHANGELOG | 0 | every entry cites an internal tracker item, an internal review finding, or a scanner alert |
| Age of `SECURITY.md` policy | since the first commit (`1684c8ca`, 2026-05-09) | `git log --diff-filter=A -- SECURITY.md` |

Because the coordinated-disclosure policy predates every release
(v0.3.0 is 2026-05-20), **no entry can be exempted as "predates the
policy"**. An exemption needs a different, recorded rationale.

## Classes

| Class | Meaning |
|---|---|
| **vuln** — MEHO-code vulnerability | A defect in code, chart, or workflow that MEHO ships which let a principal exceed its authorisation, exposed a secret or credential to a party not entitled to it, or let an outside party subvert the backplane. |
| **dep** — dependency / base-image CVE patch | Upstream CVE fixed by bumping a dependency or base-image package. The upstream project owns the advisory. |
| **hardening** — no demonstrated vulnerability | A new or strengthened control (defence in depth, fail-closed correctness, prompt-injection envelopes, resource caps, retention, CI/supply-chain pinning) where the entry demonstrates no exploitable path of the *vuln* kind. |
| **docs** — docs-only or non-vulnerability | Documentation-only entries, scanner false-positive clears, test-fixture prose. |

## Dispositions

| Disposition | Applies to | Meaning |
|---|---|---|
| **Owed — decision pending** | vuln | Per `SECURITY.md`, one of two outcomes must be recorded before the v1.0 tag: **(a)** a published GHSA (linked here), or **(b)** an exemption with a written rationale (recorded here, dated). Until one is recorded the gate is open. |
| **N/A — upstream advisory** | dep | The CVE's advisory is published by the upstream project; MEHO owes none. Out of scope per #3380. |
| **N/A — hardening** | hardening | Nothing to disclose; no affected-version / fixed-version pair exists. |
| **N/A — no vulnerability** | docs | Nothing to disclose. |

A row's *Owed* disposition becomes **Published — GHSA-xxxx-xxxx-xxxx** or
**Exempt — <dated rationale>** by editing this file in the same PR that
records the decision. Rows are never deleted.

### How an "Owed" row is decided

Every *vuln* row below was found internally (security reviews under the
private tracker, CodeQL/trivy alerts, self-review) and fixed in the
release named on the row, with the fix described publicly in the
CHANGELOG. None had an external reporter. That makes two batch
decisions defensible; the operator picks per row or per family:

1. **Publish retrospective GHSAs by weakness family** (cross-tenant
   isolation; auth / JWT validation; SSRF and peer authentication;
   credential exposure in traces, logs, or argv; approval-gate bypass;
   chart posture; UI). One GHSA per family, affected range from the
   oldest affected tag to the fixing tag, severity per CVSS 3.1,
   optional CVE request. Adopters who pin an old tag (see #2661's
   version-skew evidence) get a Dependabot / `gh` advisory signal that
   a CHANGELOG line does not give them.
2. **Record an exemption** with the rationale "internally discovered,
   fixed and publicly described in the CHANGELOG within the same
   release cycle, no external report, no known exploitation, all known
   deployments notified by the release" — acceptable only if the
   operator also confirms the private `security@meho.ai` inbox holds no
   open report matching the row (that check is an operator step; inbox
   content never lands in this public file).

Either way the decision is recorded on the row and the dated gate-2
statement on #2661 cites this file's commit.

## Ledger

Rows are in CHANGELOG order (newest first). *Line* is the heading's
line number at revision `64d1ad28`; it drifts as the CHANGELOG grows,
so the stable key is *(version, heading title / first bullet)*. Where
one heading carries several bullets the row covers all of them and the
summary says so. Internal tracker references (`meho-internal#N`) are
the private finding behind the fix.

| # | Version | Line | Summary | Class | Disposition |
|---|---|---|---|---|---|
| 1 | 0.35.4 | 157 | `vmware.composite.vm.guest.file.read` non-2xx error built from response metadata only; hostile `Content-Encoding` collapses to a fixed label; composite-count drift guard (#3718 / #3720 / #3768). | hardening | N/A — hardening |
| 2 | 0.35.3 | 176 | Login-bearing vSphere guest composites reclassified `credential_write` so the flight recorder never records bodies — safe-tier reads could retain the in-guest login password in the 30-day trace store (#3717 / #3718). | vuln | Owed — decision pending |
| 3 | 0.35.2 | 201 | Two bullets: parked `credential_write` secret values kept off approval rows and approval read surfaces via encrypted one-time hand-off (#3537 / #3664); OVF property values (appliance credentials) redacted from captured deploy bodies (#3698). | vuln | Owed — decision pending |
| 4 | 0.34.1 | 294 | Two bullets: Kubernetes `Secret` `data`/`stringData` and ArgoCD repo-credential values redacted on safe reads, closing an exfiltration path on a shared instance (#3501 / #3513) — *vuln*; httpx2 / httpcore2 bumped for CVE-2026-84382 / CVE-2026-84381 (#3526) — *dep*. | vuln (+ dep) | Owed — decision pending (bullet 1); N/A — upstream advisory (bullet 2) |
| 5 | 0.34.0 | 321 | Unknown-`kid` forced JWKS re-fetch: unsigned tokens with random `kid` drove one Keycloak discovery + JWKS round-trip per request; now bounded by a negative-kid cache and refresh cooldown (meho-internal#310 / #3484). | vuln | Owed — decision pending |
| 6 | 0.34.0 | 325 | Write-class dispatch fails closed when the durable DISPATCH audit row cannot commit (was `status="ok"` with no record) (meho-internal#295 / #3485). | hardening | N/A — hardening |
| 7 | 0.34.0 | 329 | `meho-docs` corpus endpoint screened for SSRF and the operator's Vault-capable JWT no longer replayed downstream — a `tenant_admin` could capture it (meho-internal#290 / #3483). | vuln | Owed — decision pending |
| 8 | 0.34.0 | 333 | Composite sub-op approval rows lacked `safety_level` / preview binding, so the no-self-approval-under-break-glass carve-out did not fire for a parked dangerous sub-op (meho-internal#294 / #3482). | vuln | Owed — decision pending |
| 9 | 0.34.0 | 337 | Token with no `principal_kind` defaulted to `USER` when the username-prefix marker could not fire, auto-executing caution/dangerous ops that should park; shape-based client-credentials detector added (meho-internal#299 / #3480). | vuln | Owed — decision pending |
| 10 | 0.34.0 | 341 | Fork-PR CI jobs routed off the internal runner pool; five workflows had no fork guard (meho-internal#268 / #3477). | hardening (CI) | N/A — hardening (infrastructure exposure; no shipped-artefact impact) |
| 11 | 0.34.0 | 345 | `pr-smoke.yml` buildx cache export moved to an isolated scope so a same-repo write-access PR cannot seed layers into the signed release build (meho-internal#305 / #3475). | hardening (CI) | N/A — hardening (requires a write-access insider) |
| 12 | 0.34.0 | 349 | `rke2.token.rotate` placed old and new join tokens on the `rke2` child's argv, world-readable via `/proc/<pid>/cmdline` on the server node (meho-internal#300 / #3472). | vuln | Owed — decision pending |
| 13 | 0.34.0 | 353 | `holodeck.k8s.exec` kubectl flag allowlist rejects endpoint / TLS / credential / file-write overrides before SSH — argument-level layer on the row-40 containment (meho-internal#267 / #3448). | hardening | N/A — hardening (follow-up to row 40; op already approval-gated) |
| 14 | 0.34.0 | 357 | Secret-broker `VaultKvSecretEndpoint` now enforces tenant scope before any Vault call; reachable only if the Vault ACL were also mis-provisioned (meho-internal#296 / #3445). | hardening | N/A — hardening (defence in depth) |
| 15 | 0.34.0 | 361 | Add-on heartbeat / capability writes authorised by the paired service subject — any same-tenant service token could replace another add-on's advertised surfaces by name (meho-internal#273 / #3449). | vuln | Owed — decision pending |
| 16 | 0.34.0 | 365 | Outbound connector response body capped (default 100 MiB) so an oversized vendor body cannot OOM-kill the single-replica pod (meho-internal#297 / #3459). | hardening | N/A — hardening (availability; requires a compromised registered target) |
| 17 | 0.34.0 | 369 | Image pipeline quarantines by digest, gates on trivy, promotes only the scanned digest; aliases were signed before the scan (meho-internal#284 / #3460). | hardening (CI) | N/A — hardening |
| 18 | 0.34.0 | 373 | Claude Code plugin vendors `mcp-remote` behind a committed lockfile instead of floating `npx` (meho-internal#309 / #3461). | hardening (supply chain) | N/A — hardening |
| 19 | 0.34.0 | 377 | Docs-only entry describing the human-only REST guard that shipped in #3427 (row 38) (meho-internal#289 / #3462). | docs | N/A — no vulnerability (code fix is row 38) |
| 20 | 0.34.0 | 381 | OAuth login callback bound to the initiating browser; `state` + PKCE alone allowed RFC 9700 login CSRF (victim logged into attacker's account) (meho-internal#272 / #3463). | vuln | Owed — decision pending |
| 21 | 0.34.0 | 385 | Kubernetes connector client caches keyed on `secret_ref` alone collapsed two tenants' targets onto one authenticated client; re-keyed on `(tenant_id, id)` (meho-internal#266 / #3465). | vuln | Owed — decision pending |
| 22 | 0.34.0 | 389 | `meho-docs` chunk content wrapped in the untrusted-text envelope at every LLM read boundary (meho-internal#304 / #3468). | hardening | N/A — hardening (prompt-injection isolation) |
| 23 | 0.34.0 | 393 | Bounded retention age-off for `audit_log.raw_payload` (default 90 d) — echoed secrets previously lived un-redacted indefinitely (meho-internal#307 / #3464). | hardening | N/A — hardening (retention) |
| 24 | 0.34.0 | 397 | OpenAPI-ingested op schemas emit `additionalProperties: false`; an authorised principal could append undeclared, unreviewed query params to any generic-connector request (meho-internal#293 / #3466). | vuln | Owed — decision pending |
| 25 | 0.34.0 | 401 | SSH host keys, SMTP TLS, and `meho admin keycloak` authenticated the peer before credentials are sent (`known_hosts=None`, no `ssl` context, blanket `InsecureSkipVerify` removed) (meho-internal#270 / #3467). | vuln | Owed — decision pending |
| 26 | 0.34.0 | 405 | RFC 6570 reserved expansion no longer lets an agent-supplied path value open a query / fragment or `..`-traverse to a resource other than the authorised op (meho-internal#292 / #3434). | vuln | Owed — decision pending |
| 27 | 0.34.0 | 409 | checks-investigator briefing fences target-returned sensor evidence in the untrusted-text envelope (meho-internal#302 / #3433). | hardening | N/A — hardening (prompt-injection isolation) |
| 28 | 0.34.0 | 413 | CLI interactive secret prompts suppress terminal echo (CWE-549) (meho-internal#308 / #3438). | hardening | N/A — hardening (local workstation; piped paths unaffected) |
| 29 | 0.34.0 | 417 | `audit_log` append-only enforced by a Postgres trigger (migration `0100`) (meho-internal#306 / #3435). | hardening | N/A — hardening |
| 30 | 0.34.0 | 421 | Scheduler agent-credential Vault key lacked a tenant component and lossy-sanitised the client id, so name variants or same-name-across-tenants clobbered another principal's secret (meho-internal#298 / #3436). | vuln | Owed — decision pending |
| 31 | 0.34.0 | 425 | SSRF-validated address pinned into the socket connect for dispatch and spec ingest; a resolver flipping between screen and connect (DNS rebinding) could reach a blocked address (meho-internal#275 / #3437). | vuln | Owed — decision pending |
| 32 | 0.34.0 | 429 | Approval decisions made transactional (`FOR UPDATE`) with a decision-time deadline gate; a race could let execution follow a losing / rejected / expired transition (meho-internal#274 / #3441). | vuln | Owed — decision pending |
| 33 | 0.34.0 | 433 | Broadcast subchart ships its own ingress NetworkPolicy and default-on Valkey `requirepass`; the store was reachable cluster-wide, unauthenticated, even at `networkPolicy.enabled=true` (chart is in `SECURITY.md` scope) (meho-internal#276 / #3439). | vuln (chart) | Owed — decision pending |
| 34 | 0.34.0 | 437 | Desktop `.mcpb` bundle pins transitive `qs` to ≥ 6.16.0 (GHSA-4mjr-xmp4-gh2g / CVE-2026-82417, GHSA-x5fp-wj9c-mxmx / CVE-2026-82562) (meho-internal#278 / #3444). | dep | N/A — upstream advisory |
| 35 | 0.34.0 | 441 | Passive-only kubeconfig schema rejects `exec`, `auth-provider`, and local-file refs; a tenant-controlled kubeconfig could run a subprocess, fetch tokens, or read disk inside the backplane process (meho-internal#265 / #3443). | vuln | Owed — decision pending |
| 36 | 0.34.0 | 445 | Tenant preamble bands neutralise embedded block delimiters so a `tenant_admin` cannot promote text out of the lower-trust band (meho-internal#301 / #3442). | hardening | N/A — hardening (prompt-injection isolation) |
| 37 | 0.34.0 | 449 | Sensor runner re-gates on the descriptor's current `safety_level` at dispatch; an op re-ingested to caution/dangerous after sensor creation auto-executed unattended (meho-internal#303 / #3440). | vuln | Owed — decision pending |
| 38 | 0.33.5 | 455 | Human-only governance rule enforced on REST approval-decision, grant, and agent-principal routes, not just MCP — a machine principal holding `tenant_admin` could approve, grant, or register over REST (meho-internal#289 / #3427). | vuln | Owed — decision pending |
| 39 | 0.33.5 | 459 | `docs-site` mcp-remote shim recipe used double quotes, expanding the bearer token onto the shim's argv; recipe corrected (meho-internal#262 / #3426). | docs | N/A — no vulnerability in shipped code (documentation defect; note in the client docs) |
| 40 | 0.33.5 | 463 | `holodeck.k8s.exec` (caller-supplied kubectl line) was registered `safe` / no approval; now `dangerous` + approval-gated (F05 containment) (meho-internal#267 / #3423). | vuln | Owed — decision pending |
| 41 | 0.33.4 | 483 | `result_query` `IN`-list capped at 1000 and `select` at 64 columns (resource / DoS class only) (#3389 / #3398). | hardening | N/A — hardening |
| 42 | 0.33.4 | 521 | mssql / postgres / mongodb `fingerprint` failure arm no longer embeds the raw driver exception, which carried the Vault-sourced username into `extras` and a log line (#3297). | hardening | N/A — hardening (username, not a secret) |
| 43 | 0.32.0 | 818 | HttpNfcLease device-URL disk transfers pinned to the lease `sslThumbprint`; the per-device ESXi PUTs bypassed the SSRF re-screen with no replacement control, leaving a redirect / MITM window (#3284). | vuln | Owed — decision pending |
| 44 | 0.31.0 | 2638 | Base image openssl family patched for CVE-2026-14456 via targeted `apt-get --only-upgrade` (PR #3170). | dep | N/A — upstream advisory |
| 45 | 0.29.0 | 3711 | Base image util-linux family patched for CVE-2026-53615 via targeted `apt-get --only-upgrade` (PR #3053). | dep | N/A — upstream advisory |
| 46 | 0.28.0 | 5404 | `aiohttp` 3.14.3 (CVE-2026-69244) and `cryptography` 50.0.0 (CVE-2026-69247) bumps; `cryptography` floor raised in `pyproject.toml` (#2798). | dep | N/A — upstream advisory |
| 47 | 0.23.0 | 7161 | `meho.scheduler.create` MCP tool cross-tenant write IDOR: any `tenant_admin` could schedule into any tenant; now requires `platform_admin` via `authorize_tenant_scope` (#2571). | vuln | Owed — decision pending |
| 48 | 0.23.0 | 7305 | Agent-principal register: a post-create Keycloak failure left a live, un-revocable orphan client; an over-long name registered into an unreachable kill switch (#2523). | vuln | Owed — decision pending |
| 49 | 0.23.0 | 7327 | gcloud docstring reworded to clear a permanently-open trivy `gcp-service-account` false positive (#2493). | docs | N/A — no vulnerability |
| 50 | 0.23.0 | 7339 | Workflow token least-privilege: `runner-smoke.yml` `permissions: {}`; SonarCloud job moved to GitHub-hosted with `contents: read` (CodeQL alerts #36 / #109) (#2492). | hardening (CI) | N/A — hardening |
| 51 | 0.23.0 | 7358 | Base-image digest unfrozen to a literal Dependabot-tracked pin; base pip uninstalled (CVE-2026-8643 et al.); trivy SARIF severity filter + `exit-code: 1` (#2491). | dep | N/A — upstream advisory |
| 52 | 0.20.0 | 9241 | Out-of-enum `principal_kind` claim now fails closed with `401 unknown_principal_kind` instead of coercing to a human user (issuer-controlled claim). | hardening | N/A — hardening (requires the trusted issuer to emit a bad claim) |
| 53 | 0.20.0 | 9256 | `GET /api/v1/health` gated at `OPERATOR`; `read_only` callers could trigger a Vault federation round-trip per poll; `/health/live` added (meho-internal#159). | hardening | N/A — hardening (least privilege) |
| 54 | 0.20.0 | 9280 | Targets test fixture: three operator-note remnants of a lab-default credential string replaced with `secret_ref` guidance (meho-internal#158). | docs | N/A — no vulnerability (fixture prose) |
| 55 | 0.20.0 | 9292 | Runbook verify dispatch's synthetic operator carries an empty `raw_jwt` so a Vault-backed credential read refuses locally instead of after a rejected Vault round-trip (meho-internal#157). | hardening | N/A — hardening |
| 56 | 0.20.0 | 9295 | MCP `tools/call` argument gate enforces `format` keywords (`uuid`, `date-time`) so malformed values fail as `-32602` before any handler runs. | hardening | N/A — hardening |
| 57 | 0.20.0 | 9308 | GoReleaser and VCSim images pinned to exact versions; `govulncheck` added to the CLI CI job (#155). | hardening (CI) | N/A — hardening |
| 58 | 0.20.0 | 9323 | Agent-authored stored text (broadcast / kb / memory) re-served inside the untrusted-text envelope (meho-internal#154). | hardening | N/A — hardening (prompt-injection isolation) |
| 59 | 0.20.0 | 9341 | Target-destination SSRF guard: targets could be pointed at private, loopback, link-local (cloud metadata) or reserved addresses; create/update and connect-time screens added, `MEHO_TARGET_SSRF_ALLOWLIST` opt-in (meho-internal#153). | vuln | Owed — decision pending |
| 60 | 0.20.0 | 9372 | `/mcp` 1 MiB body limit; `maxLength` on memory / knowledge tool inputs. | hardening | N/A — hardening |
| 61 | 0.20.0 | 9400 | Operator-console read path revalidates the session token on a cadence, bounding IdP revocation / role-demotion lag. | hardening | N/A — hardening |
| 62 | 0.20.0 | 9458 | Two bullets: broadcast feed Tier-1 redaction — a secret-bearing op missing from the `credential_*` allowlists shipped raw params to co-tenant feed subscribers; Tier-1 patterns and the dispatch error path no longer emit embedded credentials in cleartext. | vuln | Owed — decision pending |
| 63 | 0.20.0 | 9533 | Three bounded `holodeck-ssh` remediation write ops added behind four-eyes approval, retiring an un-audited hand-run root-SSH recovery path (#2169). | hardening | N/A — hardening (new gated feature) |
| 64 | 0.20.0 | 9537 | `harbor.robot.create` (a `credential_mint`) shipped `requires_approval=False`, so a human `tenant_admin` minted robot credentials with no second operator (#2173). | vuln | Owed — decision pending |
| 65 | 0.20.0 | 9541 | `bind9.config.apply_file` / `apply_views` (`dangerous`) shipped `requires_approval=False`, so a human `tenant_admin` could overwrite live DNS with no four-eyes step (#2126). | vuln | Owed — decision pending |
| 66 | 0.20.0 | 9641 | JWT omitting `exp` was accepted as non-expiring; now `401 missing_exp` per RFC 9068 (#2057). | vuln | Owed — decision pending |
| 67 | 0.20.0 | 9645 | `known_op_count` in the `unknown_op` error was not tenant-scoped — a cross-tenant connector-existence / op-count oracle (#2058). | vuln | Owed — decision pending |
| 68 | 0.20.0 | 9649 | KB bulk ingest confined to `KB_INGEST_ROOT`; a `tenant_admin` could ingest any `.md` on the backplane host (path traversal / LFI) (#2059). | vuln | Owed — decision pending |
| 69 | 0.20.0 | 9653 | `/ui/*` gained `Content-Security-Policy: frame-ancestors 'none'` and `X-Frame-Options: DENY`; the console shipped with no clickjacking protection (#2060). | vuln | Owed — decision pending |
| 70 | 0.20.0 | 9657 | JSONFlux DuckDB connection hardened (`enable_external_access=false`, no community extensions, config locked) — latent sink, no live path (#2061). | hardening | N/A — hardening |
| 71 | 0.20.0 | 9661 | Pooled `httpx.AsyncClient` followed redirects cross-origin, so a vendor `3xx` could forward vendor auth headers or a `307`/`308` credential body off-origin; same-origin-only loop (#2062). | vuln | Owed — decision pending |
| 72 | 0.20.0 | 9665 | `meho` CLI requires HTTPS for a routed backplane URL; plaintext `http://` only for loopback, `--insecure-allow-http` opt-in (#2063). | hardening | N/A — hardening (operator-chosen URL) |
| 73 | 0.20.0 | 9669 | Runbook step / verify `op_id` accepted `${...}` substitution, letting a run parameter redirect a published step to a different operation (#2064). | vuln | Owed — decision pending |
| 74 | 0.17.0 | 9840 | Three bullets: cross-principal memory leak on `POST /api/v1/retrieve` and the `meho://retrieve/{query}` resource (#1797); stored XSS in the runbook editor (#100); cross-session audit-replay gated at `tenant_admin` (#1843). | vuln | Owed — decision pending |
| 75 | 0.15.0 | 10350 | Vault tenant-scope guard (#1643) made default-on with a mount-pinned prefix; startup validation of the template (#1725). | hardening | N/A — hardening (defence in depth) |
| 76 | 0.14.0 | 10376 | Six bullets: cross-tenant IDOR on scheduler and retrieval routes (#1640); opt-in `vault.kv.*` tenant-scope guard (#1643); MCP `list_targets` cross-tenant enumeration (#1641); per-target credential / session caches keyed on `target.name` served one tenant another's credential or session — VCF / Harbor / NSX / SDDC (#1642), seven more connectors (#1672), shared HTTP and SSH pools (#1682). | vuln | Owed — decision pending |
| 77 | 0.3.0 | 14733 | `_remote_bash_with_sudo()` stdin discipline closes the 2026-05-04/05 bind9 credential-leak surface; repo-tree guard against any other sudo construction (#703 / #707). | vuln | Owed — decision pending |

## Totals

| Class | Rows | Disposition today |
|---|---|---|
| vuln — MEHO-code vulnerability | **38** (rows 2–5, 7–9, 12, 15, 20, 21, 24–26, 30–33, 35, 37, 38, 40, 43, 47, 48, 59, 62, 64–69, 71, 73, 74, 76, 77; row 4 also carries a dependency bump) | 38 × Owed — decision pending; 0 published; 0 exempt |
| dep — dependency / base-image CVE patch | **5** (rows 34, 44, 45, 46, 51) | N/A — upstream advisory |
| hardening — no demonstrated vulnerability | **30** (rows 1, 6, 10, 11, 13, 14, 16–18, 22, 23, 27–29, 36, 41, 42, 50, 52, 53, 55–58, 60, 61, 63, 70, 72, 75) | N/A — hardening |
| docs — docs-only or non-vulnerability | **4** (rows 19, 39, 49, 54) | N/A — no vulnerability |
| **Total** | **77** | |

**Gate 2 status at this snapshot: open.** 38 rows await a
publish-or-exempt decision and `security-advisories` returns 0. The
gate closes when every *Owed* row reads *Published* or *Exempt* and
the dated statement is posted on #2661.

## Reconciling before an rc / GA tag

Run from the repo root; the first two numbers must match, and no
*Owed — decision pending* row may remain at the GA tag.

```bash
grep -cE "^### Security" CHANGELOG.md                            # headings
grep -cE '^\| [0-9]+ \|' docs/security/advisory-ledger.md         # ledger rows
gh api /repos/evoila/meho/security-advisories --jq 'length'      # published GHSAs
grep -cE '^\| [0-9]+ \|.*Owed — decision pending' docs/security/advisory-ledger.md  # undecided rows
```

A heading count ahead of the row count means a release shipped a
`### Security` entry without a ledger row: add the row (class +
disposition) in the release-cutting PR. Every *vuln* row needs its
decision recorded before the tag it ships in is a GA tag.

## Related

- [`SECURITY.md`](../../SECURITY.md) — reporting channel, vehicle (GHSA), coordinated-disclosure steps.
- [`docs/RELEASING.md`](../RELEASING.md) — release runbook; the rc-series reconciliation step points here.
- Goal #2661 (v1.0 gates), Task #3380 (this ledger).
- Private findings behind the `meho-internal#N` references: the
  security-review records and remediation trackers on
  `evoila-bosnia/meho-internal` (e.g. Goal #87, Goal #281, Initiative
  #342). Finding detail stays there; this file carries only what the
  public CHANGELOG already states.
