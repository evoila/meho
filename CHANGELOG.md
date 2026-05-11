# Changelog

All notable changes to MEHO are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Release notes on the GitHub Releases page are generated from git
commit messages by GoReleaser (see `cli/.goreleaser.yaml`); this
top-level CHANGELOG is the human-curated narrative — what shipped,
why it matters, and what operators should pay attention to when
upgrading.

## [Unreleased]

### Added

- Multi-platform CLI release pipeline: `linux/amd64`, `linux/arm64`,
  `darwin/amd64`, `darwin/arm64` tarballs published to GitHub
  Releases on every `v*` tag push, with a combined `SHA256SUMS`
  file. Driven by GoReleaser via `.github/workflows/cli-release.yml`.

## [0.1.0] - TBD

Initial v0.1 release: backplane, CLI, Helm chart, deploy contract.

The v0.1 surface is intentionally narrow per Goal #11: enough for an
operator to install MEHO into a Kubernetes cluster, log in, and
verify the federation chain is healthy. Operations (cluster
inventory, policy enforcement, audit queries, etc.) land in v0.2+
through the CLI's server-driven discovery mechanism — adding an
operation does not require a new CLI release.

See [Goal #11](https://github.com/evoila-bosnia/meho-internal/issues/11)
for the full v0.1 scope.
