# `meho` CLI

The operator-facing command-line client for the
[MEHO governance backplane](../README.md). Single static Go binary,
multi-platform. This scaffold (G2.6-T1) ships only the `version`
subcommand; `login` and `status` arrive in subsequent G2.6 Tasks,
and post-Goal-2 operations are discovered from the backplane at
runtime.

This directory holds the Go module; the Python backplane lives at
[`../backend/`](../backend/) and the Helm chart at
[`../deploy/charts/meho/`](../deploy/charts/meho/).

## Status

**G2.6-T1 — scaffold + `meho version` only.** Login (G2.6-T2),
status + dynamic discovery (G2.6-T3), multi-platform releases
(G2.6-T4), and cosign signing (G2.6-T5) land in subsequent Tasks.

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
    ├── cmd/
    │   ├── root.go                 # cobra root + persistent flags
    │   ├── version.go              # `meho version` subcommand
    │   └── version_test.go         # output-contract test
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
