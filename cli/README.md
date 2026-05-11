# `meho` CLI

The operator-facing command-line client for the
[MEHO governance backplane](../README.md). Single static Go binary,
multi-platform. Ships `version` and `login` today; `status` and the
post-Goal-2 operations land in subsequent G2.6 Tasks.

This directory holds the Go module; the Python backplane lives at
[`../backend/`](../backend/) and the Helm chart at
[`../deploy/charts/meho/`](../deploy/charts/meho/).

## Status

**G2.6-T2 — `meho version` + `meho login`.** Status + dynamic
discovery (G2.6-T3), multi-platform releases (G2.6-T4), and cosign
signing (G2.6-T5) land in subsequent Tasks.

## Layout

```text
cli/
├── go.mod                          # module github.com/evoila/meho/cli
├── Makefile                        # build / test / lint / install
├── .golangci.yml                   # linter config (errcheck, govet, staticcheck, revive, ...)
├── cmd/
│   └── meho/
│       └── main.go                 # entry point; calls internal/cmd.Execute()
└── internal/
    ├── auth/                       # device-code flow + token storage
    │   ├── devicecode.go           # OIDC discovery + x/oauth2 device flow
    │   └── store.go                # TokenStore interface, keyring & file backends
    ├── cmd/
    │   ├── root.go                 # cobra root + persistent flags
    │   ├── version.go              # `meho version` subcommand
    │   └── login.go                # `meho login <backplane-url>` subcommand
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

## Design notes

For the durable map of what's in this module, why it's split the way
it is, and how the build flow injects identity at link time, see
[`../docs/codebase/cli.md`](../docs/codebase/cli.md).

## License

[Apache 2.0](../LICENSE). Every Go source file carries the SPDX
header `// SPDX-License-Identifier: Apache-2.0` per ADR 0003.
