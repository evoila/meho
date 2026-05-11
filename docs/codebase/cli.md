# `cli/` — meho operator CLI

> Durable map of the Go CLI module. Update in lock-step with code
> changes; stale entries are bugs.

## Overview

`cli/` houses the `meho` operator binary — a single static Go
executable that operators (and dogfooding install scripts) point at
the backplane to perform the three Goal #11 v0.1 operations: `login`,
`status`, `version`. The module is independent from the Python
backplane (`backend/`); the two communicate exclusively over the
backplane's HTTP/JSON API, with the OpenAPI spec at the seam.

As of G2.6-T2, `version` and `login` are wired. Subsequent tasks
layer in:

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
    ├── auth/
    │   ├── devicecode.go      # OAuth 2.0 device-code flow + OIDC discovery.
    │   ├── devicecode_test.go # httptest-driven flow + discovery tests.
    │   ├── store.go           # TokenStore interface + keyring/file backends.
    │   └── store_test.go      # file-fallback round-trip + 0600-mode test.
    ├── cmd/
    │   ├── root.go            # cobra root command + persistent flags.
    │   ├── version.go         # `meho version` subcommand.
    │   ├── version_test.go    # output-contract test.
    │   ├── login.go           # `meho login` subcommand + auth-config discovery.
    │   └── login_test.go      # override-resolution + help-flag tests.
    └── version/
        └── version.go         # build-time identity (Version/Commit/Date).
```

`internal/` enforces the Go-visibility seal: only packages under
`cli/` can import them. The split between `internal/cmd/` (cobra
wiring) and `internal/auth/` (flow + persistence) keeps the
cobra-aware code free of OAuth knowledge — future tasks add
`internal/backplane/` (oapi-codegen client) and `internal/output/`
(human + `--json` rendering) in the same pattern.

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

## Login flow (`meho login <backplane-url>`)

The login subcommand authenticates the operator against the
backplane's configured Keycloak realm using the OAuth 2.0 Device
Authorization Grant (RFC 8628). End-to-end shape:

1. **Auth-config discovery.** The CLI calls
   `GET <backplane-url>/api/v1/auth-config` to learn the Keycloak
   realm issuer and the OAuth `client_id` to use. The response shape
   is `{"keycloak_issuer": "...", "audience": "..."}`.
   * **Operator override.** When that endpoint isn't reachable
     (G2.2 hasn't wired it yet, the operator is behind a VPN that
     blocks the backplane but routes the IdP, etc.), pass
     `--issuer` and `--client-id` to skip discovery entirely. A
     partial override (just one flag) still hits the backplane for
     the other half.
2. **OIDC discovery.** The CLI fetches
   `<issuer>/.well-known/openid-configuration` to learn the
   `device_authorization_endpoint` and `token_endpoint`. If the
   OIDC well-known isn't published, the CLI falls back to
   `<issuer>/.well-known/oauth-authorization-server`
   (RFC 8414 OAuth 2.0 Authorization Server Metadata).
3. **Device-code initiation.** Using
   [`golang.org/x/oauth2`](https://pkg.go.dev/golang.org/x/oauth2)'s
   `Config.DeviceAuth`, the CLI POSTs `client_id` + `scope` (default
   `openid`) to the device-authorization endpoint and receives a
   `device_code`, `user_code`, `verification_uri`, and `interval`.
4. **Prompt.** The CLI prints the verification URL and `user_code` to
   stdout. The operator opens the URL on any device with a browser,
   signs in, and approves the request. (Browser auto-launch is
   deferred to a future Task per the v0.1 scope.)
5. **Polling.** `Config.DeviceAccessToken` polls the token endpoint
   at the IdP-supplied `interval`, honouring RFC 8628's
   `authorization_pending` and `slow_down` semantics. The polling
   loop returns when the IdP issues a token, the device code
   expires (`expired_token`), the operator denies the grant
   (`access_denied`), or the context is cancelled. The outer
   timeout is 10 minutes (`auth.PollTimeout`).
6. **Persistence.** The access token plus issuer, client_id,
   refresh token (captured for v0.2), and id_token are persisted to
   a backend chosen at runtime — see below.

### Token storage

`internal/auth/store.go` defines a `TokenStore` interface with two
implementations. `NewTokenStore` picks the backend at runtime:

* **OS keyring (preferred).**
  [`github.com/zalando/go-keyring`](https://github.com/zalando/go-keyring)
  abstracts Keychain (macOS), Secret Service / D-Bus (Linux), and
  Wincred (Windows). Tokens land as a single JSON blob under the
  service name `meho` keyed by the canonicalised backplane URL.
  ADR 0004 locked this library over `99designs/keyring` on
  maintenance-cadence grounds (the 99designs project has had no
  releases since December 2022; zalando is actively maintained).
* **File fallback.** When the keyring is unreachable (headless CI
  runners, sshed hosts without a D-Bus session, operators who set
  the `MEHO_KEYRING_DISABLE` escape hatch), the CLI writes to
  `$XDG_CONFIG_HOME/meho/credentials.json` (default:
  `~/.config/meho/credentials.json`). The file is created mode
  `0600` and its parent directory mode `0700` via an atomic
  tmpfile-then-rename so a partial flush can never truncate
  existing credentials.

The escape hatch (`MEHO_KEYRING_DISABLE=1`) is documented for
shared dev hosts where the local keyring belongs to a different
session — set it before `meho login` to force the file backend
unconditionally.

### What's persisted

The on-disk JSON shape (file backend) and the value stored in the
keyring (single JSON blob) are identical, keyed via `(service,
user)` where `service` is the constant `meho` and `user` is the
canonicalised backplane URL. Field set:

* `backplane_url` — the URL the token authenticates against.
* `issuer` — the Keycloak realm URL.
* `client_id` — the OAuth client used for the flow.
* `access_token` — bearer token for the backplane.
* `refresh_token` — captured for v0.2's refresh path; v0.1 never
  uses it.
* `id_token` — OIDC id_token, when issued.
* `token_type` — almost always `Bearer`.
* `expiry` — RFC3339 UTC expiration moment.

Field names are stable across CLI releases — renaming them would be
a wire-compat break for tokens persisted by older CLI versions.

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

Direct (G2.6-T2 baseline):

* `github.com/spf13/cobra` — CLI framework, per ADR 0004.
* `github.com/zalando/go-keyring` — cross-platform OS keyring,
  chosen over `99designs/keyring` (which ADR 0004 rejected on
  maintenance-cadence grounds — last release December 2022).
* `golang.org/x/oauth2` — supplies `Config.DeviceAuth` and
  `Config.DeviceAccessToken` for the RFC 8628 device-code flow.
  Pinned at `v0.26.0`, the last release that still targets Go 1.22;
  later versions require Go 1.23+ and would bump the module's go
  directive prematurely.

Indirect transitive deps tracked via `go mod tidy` in `go.sum`. The
project keeps the dep graph small — every transitive import is one
more thing supply-chain scanning has to vouch for, and operators
have to trust to run `meho login` against their secrets.

Future tasks add `github.com/oapi-codegen/runtime` (G2.6-T3 — the
generated client).

## Known issues / forward-compat scaffolding

* `meho version` prints CLI metadata only. The Goal #11 contract
  also calls for a backplane-version line, but the backplane URL
  config seam lands in G2.6-T3. Adding "not configured" as a
  placeholder string now would lock an output format that T3 then
  has to break — deferred deliberately, noted in `internal/cmd/version.go`.
* Persistent `--config` and `-v/--verbose` flags are registered on
  the root command but not yet consumed; they exist so that T3 can
  pull them via `cmd.Flags().GetString("config")` without
  retroactively restructuring the root.
* `meho login` does not yet persist a separate config file
  (`~/.config/meho/config.yaml`). The backplane URL is captured
  alongside the token in the credentials store; introducing a
  separate config file is deferred to G2.6-T3, where `meho status`
  needs an unauthenticated way to recover the backplane URL.
* The auth-config endpoint at `/api/v1/auth-config` doesn't exist on
  the backplane yet (a G2.2 coordination Task). Operators using
  this Task's binary against today's backplane must pass `--issuer`
  and `--client-id` explicitly; the prose error message guides them
  to those flags.
* Browser auto-launch (xdg-open / open) is deferred — v0.1 prints
  the URL and lets the operator copy-paste, matching how
  `gh auth login` behaves without `--web`.

## References

* Parent Goal: [#11](https://github.com/evoila-bosnia/meho-internal/issues/11)
* Parent Initiative: [G2.6 #42](https://github.com/evoila-bosnia/meho-internal/issues/42)
* Stack ADR (locked): [#13](https://github.com/evoila-bosnia/meho-internal/issues/13)
* cobra docs: https://github.com/spf13/cobra
* zalando/go-keyring: https://github.com/zalando/go-keyring
* golang.org/x/oauth2 device flow: https://pkg.go.dev/golang.org/x/oauth2#Config.DeviceAuth
* RFC 8628 — Device Authorization Grant: https://datatracker.ietf.org/doc/html/rfc8628
* golangci-lint config reference: https://golangci-lint.run/
* Empirical comparables for the scaffold pattern: `gh` (GitHub CLI),
  `argocd`, `flux`. All use cobra + ldflags-injected version, all
  ship single static binaries.
