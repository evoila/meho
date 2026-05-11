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
- **Each bullet links to the issue + PR:** `- Add Vault probe (#30 / #47)`.
  The issue number is the planning anchor (`evoila-bosnia/meho-internal`);
  the PR number is the implementation (`evoila/meho`).
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
  subchart) without real Vault + Keycloak + Postgres. Operator
  identity is faked; for real federation use the existing-k8s flow.
  (#60)
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

## [0.1.0-beta] - planned 2026-MM-DD

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
