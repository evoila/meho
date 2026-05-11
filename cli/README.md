# `meho` CLI

The operator-facing command-line client for the
[MEHO governance backplane](../README.md). Single static Go binary,
multi-platform. Ships `version`, `login`, and `status` today;
post-Goal-2 operations are server-driven (manifest fetched at
startup — see [Server-driven discovery](#server-driven-discovery)
below).

This directory holds the Go module; the Python backplane lives at
[`../backend/`](../backend/) and the Helm chart at
[`../deploy/charts/meho/`](../deploy/charts/meho/).

## Status

**G2.6-T5 — multi-platform, cosign-signed release pipeline via GoReleaser.**
Every release artefact (four tarballs + `SHA256SUMS`) ships with a
matching `.cosign.bundle` sigstore bundle on the GitHub Release. The
verification recipe lives in the [Release](#release) section below.

## Layout

```text
cli/
├── go.mod                          # module github.com/evoila/meho/cli
├── Makefile                        # build / test / lint / install / generate / snapshot
├── .golangci.yml                   # linter config (errcheck, govet, staticcheck, revive, ...)
├── api/
│   ├── openapi.json                # OpenAPI 3.0 snapshot of the backplane (input to oapi-codegen)
│   ├── oapi.config.yaml            # oapi-codegen v2 generation config
│   └── snapshot-openapi.py         # FastAPI app → openapi.json 3.0 downgrade helper
├── cmd/
│   └── meho/
│       └── main.go                 # entry point; calls internal/cmd.Execute()
└── internal/
    ├── api/
    │   ├── client.gen.go           # generated typed client (oapi-codegen v2.5)
    │   ├── client.go               # auth-aware wrapper around the generated client
    │   └── refresh.go              # lazy 401-retry refresh path
    ├── auth/
    │   ├── devicecode.go           # OIDC discovery + x/oauth2 device flow
    │   ├── store.go                # TokenStore interface, keyring & file backends
    │   └── config.go               # backplane-URL config file ($XDG_CONFIG_HOME/meho/config.json)
    ├── cmd/
    │   ├── root.go                 # cobra root + dynamic-discovery hook
    │   ├── version.go              # `meho version` subcommand
    │   ├── login.go                # `meho login <backplane-url>` subcommand
    │   └── status.go               # `meho status` subcommand + --json
    ├── discovery/
    │   └── discovery.go            # server-driven subcommand manifest fetcher
    ├── output/
    │   └── format.go               # human + JSON formatters + structured exit codes
    └── version/
        └── version.go              # build-time identity (ldflags-injected)
```

`internal/` is Go's package-visibility seal: nothing outside `cli/`
can import these packages, which keeps the CLI's surface free for
unrestrained refactoring.

## Build

```bash
cd cli/
make build          # produces bin/meho with VERSION=dev
./bin/meho version  # meho dev (commit <short-sha>, built <utc-timestamp>)
```

Release-style build with injected version metadata:

```bash
make build VERSION=v0.1.0 COMMIT=$(git rev-parse --short HEAD)
./bin/meho version
# meho v0.1.0 (commit <short-sha>, built 2026-05-10T...Z)
```

The Makefile also exposes the convenience target from the repo root
(`make cli` delegates here).

## Login

```bash
./bin/meho login https://meho.example.com
# ...prints a verification URL + user_code; open the URL on any
# device with a browser, sign in, approve, and the CLI completes.
```

Tokens persist to the OS keyring by default (Keychain on macOS,
Secret Service on Linux, Wincred on Windows). On headless hosts
without a keyring service the CLI falls back to
`$XDG_CONFIG_HOME/meho/credentials.json` (default
`~/.config/meho/credentials.json`) created mode `0600`. Set
`MEHO_KEYRING_DISABLE=1` to force the file backend explicitly.

The backplane URL is also persisted (unauthenticated) at
`$XDG_CONFIG_HOME/meho/config.json` so subsequent subcommands like
`meho status` recover it without retyping.

If the backplane hasn't yet shipped its `/api/v1/auth-config`
endpoint (G2.2 coordination), pass the realm issuer and OAuth
`client_id` explicitly:

```bash
./bin/meho login https://meho.example.com \
  --issuer https://keycloak.example.com/realms/meho \
  --client-id meho-cli
```

For the durable narrative of the login flow — discovery precedence,
polling semantics, storage backend selection, and persisted JSON
schema — see [`../docs/codebase/cli.md`](../docs/codebase/cli.md).

## Status

```bash
./bin/meho status
# Logged in as alice@example.com (sub: ...)
#   Vault: reachable, read OK (version=42)
#   DB:    migrated
```

`--json` emits a single machine-parseable JSON document on stdout —
the same shape an `install.sh` smoke test pipes through `jq`:

```bash
./bin/meho status --json | jq .
```

Exit codes:

| Code | Meaning                                                              |
| ---- | -------------------------------------------------------------------- |
| 0    | Success                                                              |
| 1    | Generic failure (cobra usage error, etc.)                            |
| 2    | `auth_expired` — no stored token, or backplane rejected the bearer   |
| 3    | `unreachable` — DNS / connection / TLS failure against the backplane |
| 4    | `unexpected_response` — backplane returned a shape outside the contract |

On `--json` mode, errors are emitted on stderr as a JSON envelope:

```json
{"error": "auth_expired", "detail": "...", "exit_code": 2}
```

The bearer token never appears in any error message — `eyJ`-prefixed
substrings are redacted on the wrapper's error-formatting path.

## Server-driven discovery

`meho` fetches `GET /api/v1/commands` from the configured backplane
at startup and registers any returned commands as dynamic cobra
subcommands. v0.1 backplanes return an empty manifest — the
scaffold runs but produces no extra commands. v0.2+ operations
land in this slot without a CLI binary release.

Fetch failures (404 before G2.2 ships the endpoint, offline
operators, etc.) degrade silently to "no extra commands" — the
local-only `login` / `status` / `version` set stays usable.

## Generated client

`internal/api/client.gen.go` is produced by
[`oapi-codegen`](https://github.com/oapi-codegen/oapi-codegen) v2.5
from `api/openapi.json`. The snapshot is committed so consumers
don't have to install the generator to build the CLI.

```bash
make tools          # installs bin/oapi-codegen v2.5.0
make generate       # regenerates internal/api/client.gen.go
make snapshot-openapi  # re-snapshot api/openapi.json from the backplane
```

`make snapshot-openapi` runs the backplane's FastAPI app under `uv`,
exports the OpenAPI document, downgrades the 3.1-specific
constructs to 3.0 (oapi-codegen v2 doesn't yet support 3.1), and
writes the result back to `api/openapi.json`. Run this whenever the
backplane's API shape changes.

A CI drift check (snapshot up to date vs. live backplane) is a
follow-up on the Initiative — see G2.7 for the workflow that will
gate PRs on it.

## Install

```bash
cd cli/
make install                # go install into $(go env GOBIN)
```

`make install` honours the same `VERSION` / `COMMIT` / `DATE`
overrides as `make build`.

## Test

```bash
cd cli/
make test           # go test -race -cover ./...
```

## Lint

```bash
cd cli/
make lint           # golangci-lint run
```

The linter set (`errcheck`, `gosimple`, `govet`, `ineffassign`,
`staticcheck`, `unused`, `gofmt`, `goimports`, `revive`) is
deliberately narrow — see comments in
[`.golangci.yml`](./.golangci.yml) for rationale. PR-level CI (lint +
test on every push) lands with Initiative G2.7 / Task #48; until then,
contributors run `make lint && make test` locally.

## Release

Release builds are driven by [GoReleaser](https://goreleaser.com/)
configured at [`.goreleaser.yaml`](./.goreleaser.yaml). On every
`v*` tag push, `.github/workflows/cli-release.yml` builds four
static binaries — `linux/amd64`, `linux/arm64`, `darwin/amd64`,
`darwin/arm64` — packages each as a `meho_<version>_<os>_<arch>.tar.gz`
tarball containing the binary plus the top-level
[`LICENSE`](../LICENSE) and this README, computes a combined
`SHA256SUMS` file, and publishes them as assets on a new draft
GitHub Release at
<https://github.com/evoila/meho/releases>.

The release is created in **draft** mode — a maintainer flips it
to public via the GitHub Releases UI after verifying the four
tarballs + matching `.cosign.bundle` files are present and
`meho version` reports the expected tag. See the [Verify signatures](#verify-signatures)
section below for the operator-side check.

### Local dry-run

```bash
cd cli/
make release-check     # validate .goreleaser.yaml (no build)
make release-dry-run   # produce dist/ tarballs + SHA256SUMS (no push)
```

Both targets install GoReleaser into `bin/` on first run (pinned to
the same v2.x line the workflow uses) and never push to GitHub.
Inspect the output under `dist/`:

```text
dist/
├── meho_0.0.1-snapshot_darwin_amd64.tar.gz
├── meho_0.0.1-snapshot_darwin_arm64.tar.gz
├── meho_0.0.1-snapshot_linux_amd64.tar.gz
├── meho_0.0.1-snapshot_linux_arm64.tar.gz
└── SHA256SUMS
```

Snapshot mode names tarballs with a synthetic `0.0.1-snapshot`
version. On a real tag push the version slot gets the tag minus
the leading `v` (`meho_0.1.0_linux_amd64.tar.gz`) while the binary's
`meho version` output preserves the full tag form (`v0.1.0`).

### Anonymous download

GitHub Releases on public repos are anonymously downloadable. After
a maintainer publishes the draft:

```bash
TAG=v0.1.0
TARBALL=meho_${TAG#v}_linux_amd64.tar.gz
curl -LO https://github.com/evoila/meho/releases/download/${TAG}/${TARBALL}
curl -LO https://github.com/evoila/meho/releases/download/${TAG}/SHA256SUMS
sha256sum -c SHA256SUMS                   # verify integrity
tar xzf ${TARBALL} && ./meho version       # meho v0.1.0 (commit ..., built ...)
```

### Verify signatures

Every release artefact is signed via cosign keyless (ADR 0006) — the
GitHub Actions OIDC token is exchanged at Fulcio for a short-lived
x509 cert bound to the workflow identity, cosign signs the artefact
digest, and the {signature, certificate, Rekor proof} triple is
attached to the GitHub Release as a single `.cosign.bundle` JSON
file. No public key to distribute, no key custody to rotate.

```bash
TAG=v0.1.0
TARBALL=meho_${TAG#v}_linux_amd64.tar.gz
BASE=https://github.com/evoila/meho/releases/download/${TAG}

curl -LO ${BASE}/${TARBALL}
curl -LO ${BASE}/${TARBALL}.cosign.bundle

cosign verify-blob \
  --certificate-identity-regexp '^https://github\.com/evoila/meho/\.github/workflows/cli-release\.yml@refs/tags/v.+$' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  --bundle ${TARBALL}.cosign.bundle \
  ${TARBALL}
# Verified OK
```

The identity-claim regex is anchored on `cli-release.yml` and the
`refs/tags/v` prefix so a malicious workflow on a fork (or a non-tag
push on this repo) cannot produce a bundle that satisfies it. Same
regex format as image (`image.yml`) and chart (`chart.yml`) signing
per ADR 0006.

The `SHA256SUMS` file is signed the same way:

```bash
curl -LO ${BASE}/SHA256SUMS
curl -LO ${BASE}/SHA256SUMS.cosign.bundle

cosign verify-blob \
  --certificate-identity-regexp '^https://github\.com/evoila/meho/\.github/workflows/cli-release\.yml@refs/tags/v.+$' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  --bundle SHA256SUMS.cosign.bundle \
  SHA256SUMS
sha256sum -c SHA256SUMS                   # then verify the tarballs
```

The two-step chain — verify the checksums file's signature first,
then `sha256sum -c` against the tarballs — lets operators verify
once and trust a whole release worth of tarballs without re-running
cosign for each one.

For the durable map of the release flow — what GoReleaser does, why
each archive includes LICENSE + README, how identity is injected,
how reproducible builds work, how the cosign signing block plugs in
— see [`../docs/codebase/cli.md`](../docs/codebase/cli.md).

## Design notes

For the durable map of what's in this module, why it's split the way
it is, and how the build flow injects identity at link time, see
[`../docs/codebase/cli.md`](../docs/codebase/cli.md).

## License

[Apache 2.0](../LICENSE). Every Go source file carries the SPDX
header `// SPDX-License-Identifier: Apache-2.0` per ADR 0003.
