# MEHO locked architecture decisions

**MEHO's foundational decision register.** These are live, load-bearing
architecture decisions that the codebase actively enforces — they are cited by
~84 places across the backend source, four Alembic migrations, tests, and docs.

> **On the numbering and the dates.** The register was first captured in a
> planning pass on **2026-05-12** (it was previously titled "v0.2 Strategic
> Decisions" and lived at `docs/planning/v0.2-decisions.md`). That origin is
> historical — **the decisions themselves are not.** The `#N` numbering is
> **load-bearing and stable**: code, migration docstrings, and docs cite these
> by number (e.g. "decision #3"), so numbers are never reused or renumbered.
> The v0.2-era planning scaffolding (release sequencing, issue-filing status)
> was stripped when the register was rehomed here; what remains is the decisions
> and the protocol for changing them.

**Decisions stay live until contradicted by a new captured decision**; do not
re-litigate without surfacing a `## Reopening discussion` block here first.

New, self-contained decisions taken since this register are filed **one per
file** in this directory (see `pgvector-superuser-prerequisite.md`,
`shipped-spec-provenance.md`, `jsonflux-license.md`). This file remains the
register for the foundational set, because its `#N` numbering is load-bearing.

---

## Locked decisions

### 1. Track count — **3 parallel tracks after G0**

After G0 (#221) ships, three producer-side tracks run in parallel:

- **Track A:** G3 (#214) — connectors, starting with G3.1 vSphere (#227)
- **Track B:** G6 (#217) — activity broadcast, starting with G6.1 SSE feed (#228)
- **Track C:** G7 (#218) — tenant conventions + Layer 2 starter (#229)

Phase 2 (G4 / G5 / G8 + G3 tier-2/3) and Phase 3 (G3 tier-4 + G9 full topology) fan in once Phase-1 tracks are in flight. Estimated **~12-15 weeks** to v0.2 ship at 3 effective tracks.

**Why:** Damir chose the most aggressive option (~30 weeks at 1 track vs ~12 weeks at 3 tracks). Requires 3 effective parallel agent/human capacity. Coordination risk acknowledged in the G0.1 (#222) Initiative body — single migration includes all the columns G3 / G6 / G7 will need so migrations don't conflict during parallel Phase 1.

### 2. G4 knowledge migration — **one-shot import + 1-month overlap**

MEHO ingests the consumer's `kb/` directory once. The repo's `kb/` and `docs/` stay live as fallback for ≥1 month. Operators retire the in-repo path only once `meho kb search` is in daily use for ≥1 month and the team agrees.

**Why:** Consumer-needs.md §G4 L121 explicitly leans this way. Single source of truth converges; the 1-month overlap absorbs migration-issue risk; dual-read-forever was rejected because it never converges. Locked in G4 Goal body (#215) DoD.

### 3. G6 broadcast PII defaults — **conservative aggregate-only for sensitive ops**

Sensitivity classifier on op_id:

- `credential_read` (initially `vault.kv.read`, `vault.kv.list`) — broadcast as `{op_id, target, result_status}`. No path, no key names, no values.
- `audit_query` (G8 verbs) — broadcast as `{op_id, result_status, row_count}`. No filter contents.
- Everything else — full request params + structured response summary.

Per-op opt-in to flip credential_read / audit_query to full detail is a G6.3 surface (separate Initiative, post-G6.1). Operator-side opt-out for a normally-full-detail op is also G6.3 territory.

**Why:** Consumer-needs.md L368 lean. Aggregate-only-by-default-for-everything kills the team-coordination UX that's G6's whole point; full-detail-by-default leaks credentials when an operator forgets to mark a read as sensitive. The classifier-on-op_id middle ground gets safe defaults with explicit per-op overrides.

### 4. G7 server-side partition — **operational rules only**

Migrated to `rdc-internal` tenant conventions (Layer 1, server-side database-backed):
- "Vault is canonical; 1Password is bootstrap-residual"
- Naming rule — no `claude` / AI-tool names in operator-visible identifiers
- Secret-handling discipline (never paste into chat, never commit secrets)
- CLI-wrapper fallback discipline during MEHO transition
- Sensitive-lab-specifics-stay-private (no real hostnames/IPs in public repo)

NOT migrated (stay in repo CLAUDE.md):
- /work-ticket flow + ticket+PR discipline
- Markdown-sidecar convention for OpenAPI specs
- PR cadence rules
- Repo-internal naming for branches / commits

**Why:** Operational rules bind ANY session against the tenant regardless of where it runs (Slack agent, MCP client, CLI on a different machine). Repo-internal rules apply only to repo work and have a filesystem of their own.

### 5. G7 Layer 2 starter — **ship `docs/examples/consumer-onboarding/CLAUDE.md` + onboarding guide**

MEHO ships a starter CLAUDE.md template in `docs/examples/consumer-onboarding/`. Consumer repos (today: `claude-rdc-hetzner-dc`; future: any external customer) copy it into their root or merge with an existing CLAUDE.md. The template teaches local Claude sessions to:

- Prefer `meho kb search` over `grep kb/`
- Prefer `meho <connector> <op>` over local `./scripts/*.sh` wrappers
- Write memories through `meho remember`, not local files
- Resolve targets through `meho targets describe`, not by reading `targets.yaml` directly

Filed as part of G7.1's scope (#229), not a separate Initiative.

**Why:** This addresses the layer the original G7 framing missed. Server-side conventions (Layer 1) bind agents connecting *through* MEHO; they don't help the operator's local Claude Code session in their cloned repo. The Layer 2 template provides consistent local-agent UX across consumer repos without per-operator divergence.

### 6. G9 topology curation — **auto-discovery + curated cross-system edges in v0.2**

G9 ships full ~100% topology in v0.2:

- **Auto-discovery (~70%):** Probes derive obvious edges (k8s cluster → nodes, vCenter → VMs, NSX → portgroups).
- **Curated cross-system edges (~30%):** Tenant-admin annotation flow + typed edges (`authenticates-via`, `routes-through`, `mounts`, `runs-on`, `depends-on`, etc.) + per-edge audit + edit conflict handling.

The edge-type vocabulary is the load-bearing modeling decision; locked early in G9 (filed when we approach Phase 3 Initiative drafting).

**Why:** Damir chose the aggressive option (full ~100% v0.2 vs auto-discovery-only). Adds ~1 Initiative to G9's original 2-Initiative scope, but unlocks topology-aware policy in v0.2.next without a deferred-curation re-litigation. Risk: cross-system edge modeling is wildcard LoE; if it slides, the auto-discovery half can still ship as v0.2.

### 7. MCP server front — **ship in v0.2 alongside CLI**

MEHO speaks MCP from v0.2 day 1. Bootstrap filed as G0.5 (#226).

**Architectural correction (2026-05-14):** The original framing of *"every G3–G9 Initiative gains a parallel MCP-tool-parity Task"* and *"every CLI command gets an MCP tool definition"* was wrong against [CLAUDE.md](../../CLAUDE.md) postulate 5. The corrected shape:

- **The agent surface is ~17 meta-tools** registered by G0.5 (`search_connectors` / `list_connectors` / `list_operation_groups` / `search_operations` / `call_operation` / `search_knowledge` / `add_to_knowledge` / `search_memory` / `add_to_memory` / `broadcast_recent` / `broadcast_announce` / `broadcast_watch` / `list_targets` / `query_topology` / `query_audit` / `result_query` / `result_aggregate` / `result_export` / `result_describe`). Per CLAUDE.md.
- **No per-vendor MCP tools.** Vendor operations (vCenter's 3,000+ paths, K8s's 13 ops, etc.) reach the agent through `call_operation`, dispatched by [#388 G0.6 operation registry](https://github.com/evoila/meho/issues/388).
- **Admin operations** use the `meho.*` namespace (`meho.broadcast.overrides.set`, `meho.audit.replay`, `meho.topology.annotate`, `meho.memory.promote`) — tenant_admin role required; not in the agent's daily surface.
- G0.5 registers the meta-tools as stubs initially; each backing Initiative (G0.6 / G4.1 / G5.1 / G6.1 / G8.1 / G9.1) ships a Task that swaps the stub for the real handler.

**Spec target:** MCP revision **2025-06-18** (current stable). Pinned in `docs/architecture/mcp.md`.

**Auth pattern:** MEHO acts as OAuth 2.1 resource server per RFC 9728 (Protected Resource Metadata) + RFC 8707 (audience binding). Keycloak (already integrated) is the authorization server. Re-uses the existing `verify_jwt` chain.

**Transport:** Streamable HTTP. Stdio explicitly NOT supported (MEHO is hosted, not a local subprocess).

**Why:** Damir chose the aggressive option (ship-in-v0.2 vs defer-to-v0.2.next). The narrow-waist meta-tool surface (post-correction) collapses the agent's tool-routing problem from "thousands of vendor-specific tools" to "~17 stable surfaces over an endpoint table." Risk: MCP spec is still iterating; pin to 2025-06-18 and document upgrade discipline.

### 8. Kubernetes connector library — **`kubernetes_asyncio`**

The Kubernetes connector (G3.2, filed against #214) uses **`kubernetes_asyncio`** as its API client:

- Async fork of the official Python Kubernetes client. Mature, maintained, broad API coverage (CoreV1 / AppsV1 / NetworkingV1 / etc.).
- Loads kubeconfig from a dict — direct fit for the `kubeconfig` field in each k8s target's Vault `secret_ref` (per the consumer's `targets.yaml` shape).
- No thread offload needed (async-native), unlike the official sync `kubernetes` client.
- Apache-2.0 licensed.

**Rejected alternatives:**
- **`kr8s`** — newer, async-first, lighter footprint, but smaller community + smaller API surface coverage. Reconsider in v0.2.next if `kubernetes_asyncio` API gaps surface.
- **`kubectl` subprocess** — matches the consumer's `kubectl-vcf.sh` shape today, but ugly architecturally + subprocess-per-op cost + harder to test.

**Why:** Kubernetes is quasi-tier-1 by usage frequency in the consumer's wrapper inventory (`kubectl-vcf.sh` is the most-used wrapper); needs its own G3 Initiative + library choice locked before Phase 1 K8s work starts. Helm is explicitly out of v0.2 K8s connector scope; future `HelmConnector` is a separate consideration.

### 9. Frontend stack — **HTMX 2 + Jinja2 + Tailwind 4 + DaisyUI 5 + Alpine.js (+ Cytoscape.js island for topology)**

Goal G10 (Operator web UI, [#336](https://github.com/evoila/meho/issues/336)) ships as a server-rendered hypermedia application served by the existing FastAPI backplane. No SPA framework, no Node toolchain in CI, no separate frontend deploy artifact.

- **HTMX 2** for partial-page swaps over the wire. GA Jun 2024; 2.x supported in perpetuity per [htmx.org/posts/2024-06-17-htmx-2-0-0-is-released](https://htmx.org/posts/2024-06-17-htmx-2-0-0-is-released/). Operator-driven actions (e.g., `meho vsphere vm.list`) render a Jinja2 template fragment that HTMX swaps into the page; no full-page reload.
- **Jinja2** for HTML templates — already FastAPI's recommended templating. One template tree per surface (broadcast feed, topology, KB search, memory, audit, connectors).
- **Tailwind CSS 4** for styling. [v4.0 GA 22 Jan 2025](https://tailwindcss.com/blog/tailwindcss-v4); CSS-first config via `@theme`; oklch palette; ~5× faster builds than v3 (Lightning CSS engine). Built once at backend-image build time via the standalone CLI binary — no `node_modules` enters CI or the image.
- **DaisyUI 5** for component primitives (cards, tables, drawers, dialogs, toasts, alerts). 50+ MIT-licensed themes; zero runtime deps; configured via `@plugin "daisyui";` directly in the main CSS file (no `tailwind.config.js`). Tailwind-4-native.
- **Alpine.js** for the modest interactive-state surfaces HTMX alone can't carry (e.g., the multi-state Memory scope-promotion modal, or the topology-table ↔ graph-selection sync flag). Imported as a single `<script>` tag; no build step.
- **Cytoscape.js** for the topology graph viz (Goal G9 / G10.5). Vendor-agnostic vanilla JS; embeds equally well as a server-rendered island on a Jinja2 page or inside any future React rewrite. Handles 1k+ node graphs.

**Rejected alternatives:**

- **React 19 + Vite 7 + TypeScript + TanStack Query/Router + shadcn/ui** — what the closed [PR #343](https://github.com/evoila/meho/pull/343) initially drafted (all of these are GA and production-ready as of May 2026). Honest pros: higher ceiling on visual polish via shadcn primitives; better mental model for a future frontend specialist; cleaner state machines for highly-interactive surfaces. Decisive cons: adds a 4th parallel CI job (Node toolchain + `package-lock.json` drift + new SHA-pin churn); the 4-person team has no frontend specialist today (`team_v0.2.md`); React → HTMX migration later is a wholesale rewrite, whereas HTMX → React is page-by-page; browser-side PKCE + sessionStorage tokens (the public-client SPA shape) is at odds with current OAuth BCP guidance (see decision #11).
- **Next.js / Remix / Svelte / Solid / Vue** — smaller ecosystems for the governance/admin UI shape than React; none change the team-fit or no-undoables calculus relative to plain React, and Next.js specifically adds a Node runtime in the chart we don't otherwise need.
- **HTML over raw FastAPI without HTMX** — every interaction full-page-reload is below the "should look very nicely" bar.

**Why:** Each of the operator's stated G10 constraints maps to HTMX over React:

1. **"Ship cleanly with backend; CI/CD must not break or become brittle."** HTMX adds **zero** new toolchains. The Tailwind 4 standalone CLI is a single `RUN` step in [`image.yml`](../../.github/workflows/image.yml); no parallel CI job; no `package-lock.json` to drift; no Node version pin to maintain. The three-job parallel pipeline in [`ci.yml`](../../.github/workflows/ci.yml) (`python-lint-test` ∥ `go-lint-test` ∥ `helm-lint-template`) stays as-is.
2. **"No undoables later."** HTMX → React migration is per-page incremental (one surface at a time can flip if there's ever a reason). React → HTMX would be the same scope as building from scratch. HTMX preserves optionality.
3. **"Not too complicated or fancy. Simplicity bias."** FastAPI + Jinja2 + HTMX is a mature 2025 production pattern for governance/admin UIs (e.g. `fasthx-admin`, server-rendered ML dashboards, IoT admin panels). React + TanStack Router + Query + state-machine glue ships substantially more concept count for the same surfaces.
4. **"Should look very nicely."** DaisyUI 5's ceiling clears the bar for a governance tool (polished tables/dialogs/cards/themes out of the box, all Tailwind-customizable). Vercel/Linear-grade polish is a designer-driven ceiling, not a framework-driven one — neither React+shadcn nor HTMX+DaisyUI gets there without a designer.
5. **"Reference visual cues from MEHO.X."** MEHO.X has no frontend (verified — `gh api /repos/evoila-bosnia/MEHO.X/contents/` shows no `frontend/` / `ui/` / `web/`). Both stacks start from a blank slate; team-fit becomes the deciding factor.

Also load-bearing: decision #11 below picks the Backend-for-Frontend auth pattern, which is a natural fit for a server-rendered stack (the FastAPI backplane *is* the BFF). Browser-side PKCE + sessionStorage tokens runs against current OWASP and the in-progress OAuth Browser-Based-Apps BCP.

### 10. Frontend deploy shape — **server-rendered from the existing FastAPI backplane at `/ui/*`; tiny static CSS+JS bundle at `/ui/static/*`**

The web UI is part of the backplane process. No separate deploy artifact, no separate ingress, no separate TLS cert, no SPA bundle.

- **Routes** at `/ui/*` resolve to FastAPI handlers that render Jinja2 templates and return HTML. HTMX partial swaps target the same surface.
- **Static assets** (one compiled `tailwind.css`, vendored `htmx.min.js`, `alpine.min.js`, `cytoscape.min.js`, logos) live under `backend/src/meho_backplane/ui/static/` and are served by FastAPI's `StaticFiles` mount at `/ui/static/*` ([FastAPI tutorial/static-files](https://fastapi.tiangolo.com/tutorial/static-files/)).
- **Build step** at backend-image build time: one `tailwindcss` CLI invocation against `backend/src/meho_backplane/ui/templates/**`. Tailwind 4's automatic content detection picks up the template tree without an explicit `content` array. Output is `tailwind.css`. No `node_modules`, no `npm`, no second image stage — the standalone Tailwind binary is downloaded with a pinned SHA256 in the Dockerfile, mirroring the kubeconform install discipline in [`ci.yml`](../../.github/workflows/ci.yml).
- **No CI changes.** Jinja2 + Tailwind output get covered by the existing `python-lint-test` job in `ci.yml`. Template linting via `djlint` is an optional follow-up via pre-commit if it pays for itself; not blocking. Backend image rebuild on FE template changes is desirable here (single deploy lifecycle, never a skew).
- **Helm chart unchanged.** [`deploy/charts/meho/`](../../deploy/charts/meho/) already terminates TLS at the backplane Service; the `/ui/*` paths are just additional FastAPI routes. No new subchart, no separate Service/Ingress/cert.

**Rejected alternatives:**

- **React static bundle served by FastAPI at `/ui/*`** (the cf4cf14 draft). Decision #9 picks server-rendered HTMX, which obviates the bundle.
- **Separate nginx subchart for the FE** — added pod, added cert/ingress/Service, independent release cadence the team doesn't yet need. Reconsider in v0.3+ if independent FE release cadence ever becomes a real constraint.

**Why:** Single deploy lifecycle (the UI always matches the backplane version it talks to — no skew); one TLS termination + one ingress + one cert; one auth boundary (the session cookie scope covers the entire app); zero new CI infrastructure. The "ship cleanly with the backend" constraint becomes literal — there's nothing else to ship.

### 11. Frontend auth — **Backend-for-Frontend (BFF) with httpOnly session cookie; backend holds all OAuth tokens**

The web UI uses the **BFF pattern**: the FastAPI backplane runs the full OAuth 2.1 Authorization Code + PKCE flow as a **confidential** client and stores all tokens server-side. The browser only ever holds a single `HttpOnly; Secure; SameSite=Strict` session cookie that binds to a server-side session record. **No JWT or refresh token ever enters the browser.**

- **Confidential OAuth client:** Keycloak registers `meho-web` as a confidential client (with secret). The secret lives in Vault under the existing CLI-client / MCP-client pattern. (`meho-cli` device-code stays unchanged; `meho-mcp` resource-server stays unchanged.)
- **Login flow:** `GET /ui/auth/login` → backplane builds an Authorization Code + PKCE request to Keycloak's `/protocol/openid-connect/auth` with `resource=<backplane-url>/api` ([RFC 8707](https://datatracker.ietf.org/doc/rfc8707/) audience binding) and redirects the browser. Keycloak callback hits `/ui/auth/callback`; backplane exchanges code+verifier for the access+refresh tokens, stores them server-side keyed by a new session ID, sets the `meho_session` cookie (`HttpOnly; Secure; SameSite=Strict; Path=/`), redirects to the originally-requested page.
- **Session storage** is server-side, Postgres-backed for restart durability (the same DB the backplane already uses). Sessions evict on logout or refresh-token rotation failure.
- **Outbound API calls** from a `/ui/*` route pull the access token from the session and forward it through the existing `verify_jwt` chain (decision #7 / G0.1). JWT validation, tenant binding, and audit-row emission are unchanged.
- **Refresh token rotation** per [RFC 9700](https://datatracker.ietf.org/doc/rfc9700/) (BCP 240, *Best Current Practice for OAuth 2.0 Security*, Jan 2025): refresh tokens are one-time-use; every exchange returns a new refresh token; replay of a used refresh token revokes the session.
- **CSRF protection:** double-submit cookie pattern on state-changing routes (POST/PATCH/DELETE). SameSite=Strict already blocks the cross-site vector; the cookie-bound CSRF token is belt-and-braces against malicious same-site sub-domains.
- **MCP auth stays separate** (decision #7). `meho-mcp` clients still authenticate against the `<backplane-url>/mcp` audience as resource server; the `/ui/*` BFF surface authenticates against `<backplane-url>/api`. The JWT `aud` claim discriminates which surface a token is for.

**Rejected alternatives:**

- **Browser-side public-client PKCE with sessionStorage tokens** (the cf4cf14 draft). Directly contradicted by current OWASP guidance — *"Do not store authentication tokens, session IDs, JWTs, refresh tokens, or any credential in localStorage or sessionStorage"* ([Session Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)) — and ranked **least secure** of three architectures in the [OAuth 2.0 for Browser-Based Apps BCP draft](https://datatracker.ietf.org/doc/html/draft-ietf-oauth-browser-based-apps), which states *"This architecture [BFF] is strongly recommended for business applications, sensitive applications, and applications that handle personal data"* — MEHO is exactly this profile.
- **Token-Mediating Backend** (the middle architecture in the BBA BCP) — backend handles OAuth but forwards the access token to the browser for direct API calls. Cleaner than browser-side PKCE; less clean than BFF because the token still enters the browser. No reason to pick the middle option when BFF is the same FastAPI app we're already running.
- **Implicit flow / hybrid flow** — removed in OAuth 2.1; not considered.
- **localStorage tokens** — same XSS exfiltration risk as sessionStorage; rejected for the same reason.

**Why:**

- **Security-correctness.** RFC 9700 (Jan 2025) is the canonical OAuth 2.0 security BCP for any new design in 2026. The OAuth Browser-Based-Apps BCP explicitly recommends BFF for sensitive applications. MEHO governs production infrastructure (Vault, Kubernetes, vCenter) — it is exactly the threat profile the BCP names.
- **Zero new attack surface in the browser.** No JWT in memory, no refresh token in memory, no silent-refresh iframe trick, no PKCE round-trip code-path to audit. XSS in the UI cannot exfiltrate a credential (cookie is `HttpOnly`).
- **Architectural fit.** Decision #9 picks server-rendered HTMX, so every navigation already involves the backend on every request — the BFF "extra hop" the pattern's critics call out doesn't exist here.
- **Future-proof against a React rewrite.** If decision #9 ever flips to React (per-page or wholesale), the BFF stays: the React SPA hits the same `/ui/auth/login` flow, holds the same `meho_session` cookie, and the backend keeps holding the tokens. The auth model is independent of the FE framework choice.
- **Reuses existing infrastructure.** Vault for the client secret; Postgres for session storage; the existing JWT chain for downstream calls. No new components to operate.

### 12. gcloud connector transport — **option B (HttpConnector + google-auth ADC + impersonation)**

Two transport options were evaluated for the GCP REST connector (G3.7-T4, #845):

- **Option A:** `SubprocessConnector` wrapping the `gcloud` CLI binary.
- **Option B:** `HttpConnector` + `google-auth` Python library, using Application Default Credentials (ADC) and `google.auth.impersonated_credentials.Credentials`.

**Decision: Option B.**

**Rejected: Option A.** `SubprocessConnector` would require the `gcloud` CLI to be installed in the backplane container image, adding ~350 MB to the image, introducing a subprocess fork per-request, and creating an out-of-band dependency on a CLI version that may diverge from the REST API version. It also precludes unit-testable auth (no injectable credentials loader).

**Why option B:**

- `google-auth` (`google-auth>=2.38`, PyPI) is a pure-Python library that works in any environment with ADC configured (GCE metadata server, GOOGLE_APPLICATION_CREDENTIALS, gcloud CLI ambient login, or Workload Identity). No container image changes beyond adding the dependency.
- `impersonated_credentials.Credentials` encodes the org-policy constraint `constraints/iam.disableServiceAccountKeyCreation` cleanly: the connector refuses SA JSON key material in any Vault `secret_ref` payload before building a token. This is a hard `ValueError`, not a warning.
- Token management (fetch + cache + 401 auto-refresh) is fully in-process, observable via structlog, and injectable in tests via `credentials_loader` + `adc_loader` arguments on `GcloudConnector`.
- Fits the existing `HttpConnector` base class — same retry policy, same `_get_json_abs` pattern already established by the Harbor connector.

**Org-policy encoding:** SA JSON key fields (`private_key`, `private_key_id`, `client_email`, `client_id`, `auth_uri`, `token_uri`, `auth_provider_x509_cert_url`, `client_x509_cert_url`) are checked by `_gate_sa_key_refusal()` on every `auth_headers()` call. Any match raises `ValueError` with the target name and offending fields.

**Recorded:** 2026-05-22 (G3.7-T4 #845).

---

## Reopening discussion

(empty)

When reopening a locked decision, add an entry here with: (1) which decision number, (2) what changed, (3) which Initiatives need re-evaluation, (4) date.
