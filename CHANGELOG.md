# Changelog

All notable changes to MEHO are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

- Multi-platform CLI release pipeline: `linux/amd64`, `linux/arm64`,
  `darwin/amd64`, `darwin/arm64` tarballs published to GitHub
  Releases on every `v*` tag push, with a combined `SHA256SUMS`
  file. Driven by GoReleaser via `.github/workflows/cli-release.yml`.
  (#46 / #178)
- Cosign keyless signing of every CLI release artefact (four
  tarballs + `SHA256SUMS`) per ADR 0006. Each artefact ships with a
  matching `.cosign.bundle` sigstore bundle (signature + Fulcio
  cert + Rekor proof, single JSON file). Verification recipe
  documented at the top-level README and `cli/README.md`. (#47)

### Changed

- GitHub Release body is now sourced from this CHANGELOG via
  `--release-notes` rather than GoReleaser's auto-generated
  git-log. The workflow extracts the section matching the current
  tag (or `[Unreleased]` as fallback). (#47)

## [0.1.0] - TBD

Initial v0.1 release: backplane, CLI, Helm chart, deploy contract.

The v0.1 surface is intentionally narrow per Goal #11: enough for an
operator to install MEHO into a Kubernetes cluster, log in, and
verify the federation chain is healthy. Operations (cluster
inventory, policy enforcement, audit queries, etc.) land in v0.2+
through the CLI's server-driven discovery mechanism — adding an
operation does not require a new CLI release.

The v0.1 trust chain across all three operator-facing artefacts —
the backplane container image, the Helm chart, and the CLI release
tarballs — is built on cosign keyless signing under a common
identity-claim format (ADR 0006). Operators verify each artefact
against the workflow path that produced it using
`cosign verify` / `cosign verify-blob` with
`--certificate-identity-regexp` — no public-key distribution, no
key custody.

See [Goal #11](https://github.com/evoila-bosnia/meho-internal/issues/11)
for the full v0.1 scope.
