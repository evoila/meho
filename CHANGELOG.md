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

## [Unreleased]

## [0.3.0] - 2026-05-20

**MVP2 — kubernetes + vault + bind9 + topology.** Five Initiatives
closed (G3.2 / G3.3 / G3.4 / G9.1 / G9.2). Three structural backstops
landed against the green-but-hollow class of failure that surfaced
during the closure push: dispatcher MRO-aware binding, registration-
time `handler_ref` resolvability guard, and the `Python (integration
testcontainers)` lane is now a required merge gate.

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
