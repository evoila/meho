# `cli/` — meho operator CLI

> Durable map of the Go CLI module at the scaffold stage. Update in
> lock-step with code changes; stale entries are bugs.

## Overview

`cli/` houses the `meho` operator binary — a single static Go
executable that operators (and dogfooding install scripts) point at
the backplane to perform the three Goal #11 v0.1 operations: `login`,
`status`, `version`. The module is independent from the Python
backplane (`backend/`); the two communicate exclusively over the
backplane's HTTP/JSON API, with the OpenAPI spec at the seam.

At the scaffold stage (G2.6-T1), only `version` is wired. Subsequent
tasks layer in:

* `meho login <backplane-url>` — OAuth 2.0 device-code flow against
  Keycloak, token storage in the OS keychain (G2.6-T2 / #44).
* `meho status` — calls `GET /api/v1/health` with the stored token,
  emits human or `--json` output, and fetches the dynamic-subcommand
  manifest for forward-compat (G2.6-T3 / #45).
* Multi-platform GoReleaser builds + tarballs to GitHub Releases
  (G2.6-T4 / #46).
* Cosign keyless signing per ADR 0006 (G2.6-T5 / #47).

## Module layout

```text
cli/
├── go.mod                  # github.com/evoila/meho/cli; Go 1.22.
├── Makefile                # build / test / lint / install / clean.
├── .golangci.yml           # linter config (rationale below).
├── .gitignore              # bin/, dist/, coverage artefacts.
├── README.md               # user-facing quickstart.
├── cmd/
│   └── meho/
│       └── main.go         # entry point; exits 1 on error.
└── internal/
    ├── cmd/
    │   ├── root.go         # cobra root command + persistent flags.
    │   ├── version.go      # `meho version` subcommand.
    │   └── version_test.go # output-contract test.
    └── version/
        └── version.go      # build-time identity (Version/Commit/Date).
```

`internal/` enforces the Go-visibility seal: only packages under
`cli/` can import them. The split between `internal/cmd/` (cobra
wiring) and `internal/version/` (data) keeps the cobra-aware code
free of build-metadata logic — future tasks add `internal/keyring/`,
`internal/backplane/`, and `internal/output/` in the same pattern.

The `cmd/meho/main.go` entry point is intentionally minimal — it
calls `cmd.Execute()` and translates the returned error into an exit
code. All command construction happens inside `internal/cmd/` so the
top-level `main` package never carries logic that needs unit testing.

## Build flow

The Makefile is the single source of truth for build invocations:

| Target | What it does |
| --- | --- |
| `make build` | Compiles `bin/meho` with ldflags-injected version metadata. |
| `make test` | Runs `go test -race -cover ./...`. |
| `make lint` | Runs `golangci-lint run` against `.golangci.yml`. |
| `make tidy` | Synchronises `go.mod` / `go.sum` with imports. |
| `make install` | Installs into `$(go env GOBIN)` (or `$GOPATH/bin`). |
| `make clean` | Removes `bin/` and `dist/`. |

Build-time identity injection follows the canonical Go pattern (the
one `kubectl`, `gh`, `argocd`, `flux` all use):

```bash
LDFLAGS="-X github.com/evoila/meho/cli/internal/version.Version=v0.1.0 \
         -X github.com/evoila/meho/cli/internal/version.Commit=abc1234 \
         -X github.com/evoila/meho/cli/internal/version.Date=2026-05-10T12:00:00Z"
go build -trimpath -ldflags "-s -w $LDFLAGS" -o bin/meho ./cmd/meho
```

The Makefile shells out to `git rev-parse --short HEAD` and `date -u`
for the `COMMIT` / `DATE` defaults, so a contributor running plain
`make build` still gets a binary that identifies itself with the
real commit it was produced from. Release builds (G2.6-T4) override
`VERSION` with the semver tag and re-emit the same Makefile via
GoReleaser.

`-trimpath` strips the build-machine path prefix from the binary,
which is required for reproducible builds and avoids leaking the
build directory into stack traces. `-s -w` strips the symbol table
and DWARF debug info — Go's runtime still emits useful panics (it
uses PC-only stack walks).

## Lint configuration rationale

`.golangci.yml` enables nine linters — the six golangci-lint runs by
default (`errcheck`, `gosimple`, `govet`, `ineffassign`,
`staticcheck`, `unused`) plus `gofmt`, `goimports`, and `revive`.

Choices deliberately omitted:

* **`gochecknoglobals`** — the build-time identity vars in
  `internal/version` are intentionally package-level; ldflags can
  only inject into globals.
* **`exhaustruct`** — cobra command literals omit dozens of optional
  fields by design; enforcing exhaustive initialisation would force
  noise without catching real bugs.
* **`wrapcheck` / `err113`** — CLI exit-status handling routinely
  returns sentinel errors from third-party packages unmodified;
  wrapping every error would be cargo-culted hygiene without
  improving operator output.
* **`gocyclo` / `cyclop`** — premature at the scaffold stage; will
  be revisited when subcommand RunE functions grow real branches.

The exclude-rules block relaxes `errcheck` and `revive` on
`_test.go` files only — test code routinely uses blank identifiers
and dot imports in ways production code shouldn't.

## Dependencies

Direct: `github.com/spf13/cobra` (CLI framework, per ADR 0004 —
the stack-choice ADR; ADR 0001 covers license choice and is
unrelated). Indirect transitive deps tracked via `go mod tidy` in
`go.sum`. The project intentionally keeps the dep graph small —
every transitive import is one more thing supply-chain scanning has
to vouch for, and operators have to trust to run `meho login`
against their secrets.

Future tasks add `github.com/zalando/go-keyring` (G2.6-T2 — chosen
over `99designs/keyring`, which ADR 0004 rejected on
maintenance-cadence grounds; the same ADR specifies a file-backed
fallback at `~/.config/meho/credentials` mode `0600` for hosts with
no OS keyring service), `github.com/oapi-codegen/runtime` (G2.6-T3 —
the generated client), and the OAuth 2.0 device-code helper from
`golang.org/x/oauth2` (G2.6-T2).

## Known issues / forward-compat scaffolding

* `meho version` prints CLI metadata only. The Goal #11 contract
  also calls for a backplane-version line, but the backplane URL
  config seam lands in G2.6-T3. Adding "not configured" as a
  placeholder string now would lock an output format that T3 then
  has to break — deferred deliberately, noted in `internal/cmd/version.go`.
* Persistent `--config` and `-v/--verbose` flags are registered on
  the root command but not yet consumed; they exist so that T2/T3
  can pull them via `cmd.Flags().GetString("config")` without
  retroactively restructuring the root.

## References

* Parent Goal: [#11](https://github.com/evoila-bosnia/meho-internal/issues/11)
* Parent Initiative: [G2.6 #42](https://github.com/evoila-bosnia/meho-internal/issues/42)
* Stack ADR (locked): [#13](https://github.com/evoila-bosnia/meho-internal/issues/13)
* cobra docs: https://github.com/spf13/cobra
* golangci-lint config reference: https://golangci-lint.run/
* Empirical comparables for the scaffold pattern: `gh` (GitHub CLI),
  `argocd`, `flux`. All use cobra + ldflags-injected version, all
  ship single static binaries.
