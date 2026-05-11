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

As of G2.6-T5 the v0.1 trio is wired (`version`, `login`, `status`)
and the multi-platform release pipeline ships every `v*` tag as
four tarballs + `SHA256SUMS` to GitHub Releases via GoReleaser. Each
tarball and `SHA256SUMS` ships with a matching `.cosign.bundle`
sigstore bundle (signature + Fulcio cert + Rekor proof) under the
ADR 0006 identity-claim format. Server-driven subcommand discovery
runs at every startup (empty manifest in v0.1; populated by
post-Goal-2 backplanes without a CLI binary release).

## Module layout

```text
cli/
├── go.mod                  # github.com/evoila/meho/cli; Go 1.22.
├── Makefile                # build / test / lint / install / generate / snapshot / release.
├── .golangci.yml           # linter config (rationale below).
├── .goreleaser.yaml        # GoReleaser v2 release config (rationale: § Release pipeline).
├── .gitignore              # bin/, dist/, LICENSE-copy, coverage artefacts.
├── README.md               # user-facing quickstart.
├── api/
│   ├── openapi.json        # OpenAPI 3.0 snapshot — input to oapi-codegen.
│   ├── oapi.config.yaml    # oapi-codegen v2 generation config.
│   └── snapshot-openapi.py # FastAPI → 3.0-downgrade helper.
├── cmd/
│   └── meho/
│       └── main.go         # entry point; honours output.ExitCoder.
└── internal/
    ├── api/
    │   ├── client.gen.go      # generated typed client (oapi-codegen v2.5).
    │   ├── client.go          # auth-aware wrapper; NewAuthedClient + GetHealth.
    │   └── refresh.go         # lazy 401-retry refresh via x/oauth2.
    ├── auth/
    │   ├── devicecode.go      # OAuth 2.0 device-code flow + OIDC discovery.
    │   ├── devicecode_test.go # httptest-driven flow + discovery tests.
    │   ├── store.go           # TokenStore interface + keyring/file backends.
    │   ├── store_test.go      # file-fallback round-trip + 0600-mode test.
    │   ├── config.go          # backplane-URL config file ($XDG/meho/config.json).
    │   └── config_test.go     # roundtrip + 0600/0700 mode test.
    ├── cmd/
    │   ├── root.go            # cobra root + dynamic-discovery hook.
    │   ├── root_test.go       # built-in command surface + dynamic-graft test.
    │   ├── version.go         # `meho version` subcommand.
    │   ├── version_test.go    # output-contract test.
    │   ├── login.go           # `meho login` subcommand + auth-config discovery + config persistence.
    │   ├── login_test.go      # override-resolution + help-flag tests.
    │   ├── status.go          # `meho status` subcommand + --json + URL resolver.
    │   └── status_test.go     # happy/JSON/no-creds/unreachable/401/redaction tests.
    ├── discovery/
    │   ├── discovery.go       # /api/v1/commands manifest fetch + cobra graft.
    │   └── discovery_test.go  # 200/404/transport/decode + collision tests.
    ├── output/
    │   ├── format.go          # human + JSON formatters + structured exit codes.
    │   └── format_test.go     # human/JSON/exit-code pinning.
    └── version/
        └── version.go         # build-time identity (Version/Commit/Date).
```

`internal/` enforces the Go-visibility seal: only packages under
`cli/` can import them. The split between `internal/cmd/` (cobra
wiring) and `internal/auth/` (flow + persistence) keeps the
cobra-aware code free of OAuth knowledge. `internal/api/`,
`internal/discovery/`, and `internal/output/` follow the same
pattern — each owns one well-defined concern (typed HTTP surface,
manifest discovery, formatted output discipline) and exposes a
small API to the cobra layer.

The `cmd/meho/main.go` entry point honours the `output.ExitCoder`
interface: any error returned from a subcommand's `RunE` that
satisfies `ExitCoder` (which is every `output.StructuredError`)
gets its `ExitCode()` propagated as the process exit code. Anything
else falls back to exit 1.

## Build flow

The Makefile is the single source of truth for build invocations:

| Target | What it does |
| --- | --- |
| `make build` | Compiles `bin/meho` with ldflags-injected version metadata. |
| `make test` | Runs `go test -race -cover ./...`. |
| `make lint` | Runs `golangci-lint run` against `.golangci.yml`. |
| `make tidy` | Synchronises `go.mod` / `go.sum` with imports. |
| `make install` | Installs into `$(go env GOBIN)` (or `$GOPATH/bin`). |
| `make clean` | Removes `dist/` and the meho binary (keeps `bin/oapi-codegen`). |
| `make tools` | Installs `bin/oapi-codegen` at the pinned v2.5.0. |
| `make generate` | Regenerates `internal/api/client.gen.go` from `api/openapi.json`. |
| `make snapshot-openapi` | Re-snapshots `api/openapi.json` from the backplane's FastAPI app. |
| `make goreleaser` | Installs `bin/goreleaser` at the pinned v2.15.4. |
| `make release-check` | Runs `goreleaser check` against `.goreleaser.yaml` (config-only validation). |
| `make release-dry-run` | Runs `goreleaser release --snapshot --clean --skip=publish` for a local rehearsal (no push). |

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
`VERSION` with the git tag form via GoReleaser's `{{.Tag}}` template;
see the **Release pipeline** section below for the full ldflags
binding rationale.

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
* `refresh_token` — exchanged for a fresh access_token by
  `meho status` on a 401 response (T3). Captured at login time so
  the refresh path lands without a token-store schema migration.
* `id_token` — OIDC id_token, when issued.
* `token_type` — almost always `Bearer`.
* `expiry` — RFC3339 UTC expiration moment.

Field names are stable across CLI releases — renaming them would be
a wire-compat break for tokens persisted by older CLI versions.

### Backplane-URL config file (`config.json`)

The credentials store keys entries by `(service, user)` where
`user` is the backplane URL — fine for `meho login` (the operator
passes the URL on the command line), unworkable for `meho status`
(which has no URL to type). T3 introduces an unauthenticated
companion file at `$XDG_CONFIG_HOME/meho/config.json` carrying
the operator's preferred backplane URL:

```json
{ "backplane_url": "https://meho.evba.lab" }
```

`meho login` writes this file at the end of a successful flow;
`meho status` reads it to learn which backplane to query. The file
contains no secrets, but it lives in the same directory as
`credentials.json` and inherits the `0600` / `0700` posture so a
single `chmod -R 0700 ~/.config/meho/` covers both files
identically.

Operators can override per invocation: `meho status --backplane <url>`
bypasses the config file. Useful for ad-hoc queries against a
second environment without re-running login.

`auth.LoadConfig` returns `auth.ErrConfigNotFound` when the file
doesn't exist, which the cobra command translates into a friendly
`auth_expired` error pointing at `meho login`.

## Status flow (`meho status`)

`meho status` exercises the entire backplane stack end-to-end: it
calls `GET /api/v1/health` with the stored bearer token, the
backplane validates the JWT against Keycloak, forwards it to Vault
via JWT/OIDC federation, reads a sentinel secret, probes the DB
migration state, and returns a structured response. The CLI
renders that response in one of two formats.

### Pipeline

1. **Resolve the backplane URL.** Override (`--backplane <url>`)
   first, otherwise read from `$XDG_CONFIG_HOME/meho/config.json`.
   Missing config → `auth_expired` with a `meho login <url>` hint.
2. **Build the AuthedClient.** `api.NewAuthedClient` loads the
   stored token via `auth.NewTokenStore`, wraps it in a
   `RequestEditorFn` that stamps `Authorization: Bearer <token>`
   on every outbound request, and assembles the generated
   `ClientWithResponses`. Token-not-found surfaces as
   `auth_expired`.
3. **Call the typed endpoint.** `AuthedClient.GetHealth(ctx)` is a
   small wrapper around the generated
   `AuthenticatedHealthApiV1HealthGetWithResponse` that adds a
   one-shot 401-retry refresh:
   * On 200, returns the typed `HealthResponse`.
   * On 401, attempts an `oauth2.TokenSource`-driven refresh
     using the persisted `refresh_token` + the issuer URL captured
     at login time. If refresh succeeds, retries the request once.
   * If refresh fails (no refresh_token, IdP rejected), surfaces
     `auth_expired`.
4. **Render output.** Default human format on stdout; `--json`
   emits the typed body as a single JSON document. Both formats
   write success output to stdout and errors to stderr.

The refresh exchange persists the new access_token (and rotated
refresh_token, if the IdP issued one) back to the token store on
a best-effort basis — a save failure doesn't break the in-flight
request, which already has the new bearer in its header.

### Output discipline (Goal #11 §5)

Output discipline is **mandatory**, not optional. The
dogfooding `install.sh` smoke test pipes `meho status --json`
through `jq` to verify the federation chain. Three rules govern
every output path:

1. **Success → stdout. Failure → stderr.** Operators redirect
   stdout to a file or pipe and expect it to be free of error
   noise; `2>/dev/null` should leave a working data stream behind.
2. **Default human format on stdout; `--json` swaps in a single
   JSON document.** The JSON path is parseable by `jq` end to end
   (one trailing newline, no log lines, no warnings on stdout).
3. **Errors are structured.** Every failure path produces an
   `output.StructuredError` with a stable string code
   (`auth_expired`, `unreachable`, `unexpected_response`) and a
   numeric exit code (2/3/4). On `--json` mode the error surfaces
   as a JSON envelope on stderr:

   ```json
   {"error": "auth_expired", "detail": "...", "exit_code": 2}
   ```

The exit codes are wire-contract — renumbering breaks consumers
that branch on them:

| Code | Meaning                                                         |
| ---- | --------------------------------------------------------------- |
| 0    | Success                                                         |
| 1    | Generic failure (cobra usage error, panic recovery, etc.)       |
| 2    | `auth_expired`                                                  |
| 3    | `unreachable`                                                   |
| 4    | `unexpected_response`                                           |

The propagation path is `output.RenderError` → `silentError` →
`cmd.Execute` → `main.go` → `os.Exit(coder.ExitCode())`. cobra's
default error printer is bypassed via `SilenceErrors=true` on the
status subcommand so the JSON envelope doesn't double-render
alongside the text rendering of `.Error()`.

### Sensitive-data discipline

The bearer token is the only credential the CLI handles and it
**never** appears in operator-visible output. Three layered
defences enforce that:

1. **PrintHealth doesn't see the token.** The renderer takes only
   the typed `HealthResponse` (which contains no bearer); the
   bearer rides in the request header, not the response body.
2. **Error paths redact `eyJ`-prefixed substrings.** The `unreachable`
   path runs every wrapped error through `redactedError` before
   surfacing it, replacing any whitespace-bounded JWT-shaped
   field with `[redacted-token]`. The `eyJ` prefix is the base64-URL
   header every JWT emits — if a transport-layer library ever
   leaks the bearer into an error message (an http.Request URL with
   the token embedded, for instance), this scrub catches it.
3. **The output_test.go discipline test pins it.** A test seeds a
   stored token with the literal marker
   `eyJ.TEST-DUMMY-TOKEN-MARKER.SHOULD-NEVER-APPEAR`, runs the
   full status pipeline against a mock backplane, and asserts the
   marker does not appear in stdout or stderr regardless of
   `--verbose`. Any future regression that surfaces the bearer
   fails this test.

The same `eyJ` prefix matches access tokens, refresh tokens, and
id tokens alike, so the single redaction rule covers every
credential the CLI persists.

## Generated client (`internal/api/`)

`internal/api/client.gen.go` is produced by
[oapi-codegen v2.5](https://github.com/oapi-codegen/oapi-codegen)
from `cli/api/openapi.json` — a committed snapshot of the
backplane's OpenAPI document. v2.5 is the last v2.x release with
Go 1.22 minimum; later versions require Go 1.24+ and would bump
the module's `go` directive prematurely. The generator itself runs
on a newer Go toolchain (downloaded automatically by `go install`
when the host has Go 1.21+) so this is a build-time vs.
runtime split.

### Snapshot pipeline

The backplane is FastAPI; FastAPI emits OpenAPI 3.1 at runtime via
`/openapi.json`. oapi-codegen v2 doesn't yet support OpenAPI 3.1
(upstream issue 373) so the snapshot pipeline runs a 3.1 → 3.0
downgrade on the way out:

1. `make snapshot-openapi` shells `uv run python ../cli/api/snapshot-openapi.py`
   from `../backend/`.
2. The script imports `meho_backplane.main.app`, calls
   `app.openapi()` to get the 3.1 document, then applies two
   transforms:
   * Rewrites `"openapi": "3.1.x"` to `"openapi": "3.0.3"`.
   * Collapses `anyOf: [<schema>, {"type": "null"}]` (FastAPI's
     encoding for `Optional[T]`) to `{<schema>, "nullable": true}`
     (the 3.0 idiom).
3. The result lands at `cli/api/openapi.json`, committed.

Both transforms are lossless for v0.1's spec. If a richer 3.1
construct ever lands on the backplane (the `type: ["string","null"]`
array form, tuples via `prefixItems`, etc.), extend `snapshot-openapi.py`
alongside the change. A CI drift check that re-snapshots and
diffs against the committed copy is a G2.7 follow-up.

### Generation

```bash
make tools          # installs bin/oapi-codegen v2.5.0
make generate       # regenerates internal/api/client.gen.go
```

The generated file is committed. Consumers building from source
don't need `oapi-codegen` installed; only contributors who touch
the API surface re-run `make generate`.

`cli/api/oapi.config.yaml` controls what gets generated:

* `package: api` — the generated file lands in `internal/api/`.
* `output: internal/api/client.gen.go` — single file.
* `generate.models: true` — typed Go structs for every schema.
* `generate.client: true` — both `Client` (per-operation methods
  returning `*http.Response`) and `ClientWithResponses` (per-operation
  methods returning typed `JSON200` / `JSON401` / `JSON422` fields).

### Auth-aware wrapper (`client.go`)

`api.NewAuthedClient(backplaneURL, opts)` wraps the generated
`ClientWithResponses` with:

* **Bearer injection.** A `WithRequestEditorFn` reads the current
  access_token from a `tokenBox` under a mutex on every outbound
  request and stamps `Authorization: Bearer <token>`.
* **Lazy 401-retry refresh.** `AuthedClient.GetHealth(ctx)` calls
  the generated endpoint; on 401, the `tokenBox.refresh(ctx)` path
  runs OIDC discovery against the issuer URL stored at login time,
  builds an `oauth2.Config` with the discovered `token_endpoint`,
  and exchanges the stored `refresh_token` for a fresh
  access/refresh pair. The bearer header swap-out happens atomically
  under the box's mutex; a concurrent invocation sees either the
  old or the new token, never a torn string.
* **Persistence.** On a successful refresh the new token is written
  back to the same `TokenStore` the CLI loaded it from
  (best-effort: a save failure doesn't break the in-flight request).

The refresh path is exercised end to end in `status_test.go`'s
401 scenarios (the no-refresh-token branch surfaces `auth_expired`;
the present-refresh-token branch isn't yet exercised under unit
test because mocking Keycloak's well-known + token-exchange is
heavyweight — that path is covered by the G2.8 integration suite).

## Server-driven discovery (`internal/discovery/`)

Goal #11 §5 mandates server-driven `--help`: adding an operation to
the backplane shouldn't require a new CLI binary release. v0.1
ships the scaffold; v0.2+ populates it.

### Pipeline

1. `cmd.newRootCmd` registers built-in subcommands (`version`,
   `login`, `status`) first, then calls
   `registerDynamicSubcommands(root)`.
2. The function loads `auth.LoadConfig` to discover the operator's
   preferred backplane URL. Missing config → no fetch.
3. `discovery.Fetch(ctx, http.DefaultClient, backplaneURL)` GETs
   `/api/v1/commands` under a 5-second timeout cap.
4. Every failure mode — transport error, non-2xx response — yields
   an empty manifest, **not** an error. Operators offline, behind a
   broken VPN, or against a v0.1 backplane (which returns 404 for
   `/api/v1/commands`) all see "no dynamic commands" silently.
5. On a 2xx response with a decodable body,
   `discovery.Register(root, manifest)` grafts each manifest entry
   onto the cobra tree as a dynamic subcommand. Each leaf
   subcommand's `RunE` is a v0.1 placeholder that prints
   "operation not yet implemented locally; upgrade the meho CLI"
   — the v0.1 backplane never populates the manifest, so this only
   fires for forward-rolled scenarios.

### Manifest shape

```json
{
  "commands": [
    {
      "name": "k8s",
      "short": "Kubernetes operations",
      "subcommands": [
        { "name": "list", "short": "List managed clusters" }
      ]
    }
  ]
}
```

Field names are stable across CLI releases. v0.2 adds `usage`,
`flags`, and `args` descriptors so dynamic commands can replay
the operator's intent server-side; v0.1 CLIs running against v0.2
backplanes ignore the new fields gracefully (`encoding/json`'s
default unknown-key behaviour).

### Collision protection

`discovery.Register` refuses to graft a manifest command whose
name matches an already-registered built-in (`login`, `status`,
`version`). A misconfigured backplane that advertised
`{"name": "login"}` would otherwise shadow the real login
subcommand — a security footgun in the making. The collision
error surfaces as a stderr warning during startup; the rest of the
manifest still registers.

### Test-only seam

`cmd.setDynamicRegistrar(fn)` is the test-only override of the
registrar. Tests use it to inject synthetic manifests
deterministically without standing up a real backplane HTTP
server. `root_test.go` exercises the mock-`k8s`-manifest scenario
the issue body's acceptance criterion calls for.

### v0.1 limitations

* No caching. Every CLI invocation fetches the manifest, which
  costs one round-trip per `meho` call. v0.2 adds a TTL cache at
  `~/.meho/commands-cache.json` (per the issue body's deferred
  scope).
* No shell completion driven by the manifest. cobra's static
  completion still works for the built-in commands; dynamic
  completion is a v0.2 enhancement.
* No backplane endpoint yet. `/api/v1/commands` is a coordination
  point with G2.2/G2.7 — until it ships, every fetch falls back
  to the empty-manifest path.

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

## Release pipeline

Release builds are driven by [GoReleaser](https://goreleaser.com/)
v2 configured at `cli/.goreleaser.yaml`, executed by
`.github/workflows/cli-release.yml`. The pipeline trades the
hand-rolled GitHub Actions matrix that `gh release create` would
require for GoReleaser's single-config model — the same shape `gh`,
`argocd`, and `flux` all use. Cosign keyless signing (ADR 0006)
attaches a sigstore bundle to every release artefact in the same
GoReleaser invocation; the `signs:` block runs after `archives:`
and `checksum:`, so the same single workflow produces both the
artefacts and the signatures atomically.

### Trigger surface

The workflow runs only on `v*` tag push. Goal #11's release
contract is explicit that the tag is the authoritative version
stamp — a push to main without a tag has no semver to bake into
the binary or the tarball file name, so we don't run. The
`concurrency` group is keyed on `github.ref` so a fast-follow
re-tag (force-pushed `v0.1.0-rc.1` during validation, for
instance) cancels its predecessor cleanly.

Permissions follow the per-job least-privilege posture the rest of
the workflows use (chart.yml, image.yml):

* Workflow-default `contents: read` — just enough to checkout.
* `release` job elevates to `contents: write` (GitHub Release
  creation) + `id-token: write` (cosign keyless OIDC; cosign
  exchanges this token at Fulcio for a ~10-minute x509 cert bound
  to the workflow identity).

Only the release job carries the elevated scopes — the
workflow-default `contents: read` stays as the floor.

### Build matrix

GoReleaser's `builds` block expands the 2×2 target matrix on a
single x86_64 runner — Go's cross-compilation is built-in, so the
darwin/* and arm64 targets are first-class without QEMU or a
multi-arch runner pool. Every target uses:

* `CGO_ENABLED=0` — pure-Go static binary; no glibc dep, single
  tarball ships unmodified to any operator's machine.
* `-trimpath` + `-s -w` — same flags `cli/Makefile`'s release path
  uses (strips build-machine path prefix + symbol table + DWARF
  debug info).
* `mod_timestamp: {{ .CommitTimestamp }}` — pins file modification
  times inside the tarball to the commit author date, so a rebuild
  of the same tag produces a byte-identical binary. Required for
  cosign attestation where the signed digest must match between
  independent builds; the `signs:` block (see below) hashes the
  artefact content, so a non-reproducible build would break
  signature verification on the second run.
* `ldflags -X github.com/evoila/meho/cli/internal/version.{Version,Commit,Date}` —
  feeds `internal/version/version.go`. Bindings:
  - `Version → {{.Tag}}` (preserves the leading `v` per Goal #11
    acceptance criterion; GoReleaser's default `{{.Version}}` strips
    it for the file-name slot).
  - `Commit → {{.Commit}}` (full SHA on a release binary; the
    Makefile's `make build` path uses the short form for dev).
  - `Date → {{.CommitDate}}` (commit author date, not build wall
    clock — required for reproducibility).

### Archive layout

Each `<os>/<arch>` target produces one tarball:

```text
meho_<version-no-leading-v>_<os>_<arch>.tar.gz
├── meho           # the static binary
├── LICENSE        # top-level Apache 2.0 (copied into cli/ by the
│                  # before-hook; cli/.gitignore excludes the copy
│                  # from git — source of truth stays at repo root)
└── README.md      # the cli/ user-facing README
```

GoReleaser's archive globs forbid `..` path traversal by design (a
defence against pulling arbitrary host files into release tarballs),
which is why the top-level `LICENSE` is hoisted into `cli/` via a
`before:` hook (`sh -c 'cp ../LICENSE LICENSE'`) before the archive
step runs. The copy is gitignored; the source of truth remains the
repo-root `LICENSE`.

The combined `SHA256SUMS` file is produced by
`checksum: name_template: 'SHA256SUMS'`. Operators verify with:

```bash
sha256sum -c SHA256SUMS
```

### Tag → version slot

GoReleaser strips the leading `v` from the git tag for the
file-name version slot (per the `{{ .Version }}` template
default — semver body convention), so `v0.1.0` produces
`meho_0.1.0_linux_amd64.tar.gz`. The binary's `meho version`
output preserves the full tag form (`v0.1.0`) via the
`{{ .Tag }}` binding documented above. Both forms exist for a
reason: file names benefit from the strict-semver shape that some
package managers expect (homebrew-releaser is the v0.2 driver here);
runtime identity benefits from human readability (`v` prefix).

### Release notes

The top-level `CHANGELOG.md` (Keep a Changelog format) is the
authoritative source of release-note text. The workflow's
`Extract release notes from CHANGELOG.md` step pulls the section
matching the current tag's version (`## [0.1.0]`) — with
`## [Unreleased]` as a fallback for pre-release tags — into
`$RUNNER_TEMP/release-notes.md`, then passes the path via
`--release-notes` to GoReleaser. GoReleaser uses the file content
verbatim as the GitHub Release body, overriding its built-in
`changelog:` git-log generation.

`cli/.goreleaser.yaml` keeps the `changelog: use: git` block as a
fallback for snapshot builds (`make release-dry-run` doesn't pass
`--release-notes`). The `groups` block there maps Conventional
Commits prefixes to release-note sections (`feat:` → Features,
`fix:` → Fixes, everything else → Other) — matches the allowed
prefix set in `.pre-commit-config.yaml`. Dependabot churn
(`chore(deps): Bump …`) and merge commits are filtered out so the
fallback release notes stay readable too.

The CHANGELOG.md discipline (one bullet per merged PR, ticket+PR
links, Keep-a-Changelog categories — see the "How entries are
added" section in CHANGELOG.md itself) means the release body is
deterministic and reviewable in a PR before a tag is cut, rather
than reconstructed at tag time from commit messages.

### Cosign signing (ADR 0006)

GoReleaser's `signs:` block runs after `archives:` and `checksum:`,
so the `artifacts: all` glob covers every file destined for the
GitHub Release — the four tarballs **and** the combined `SHA256SUMS`
file. Per artefact, cosign produces a single `.cosign.bundle` JSON
file containing the signature, the Fulcio-issued certificate, and
the Rekor transparency-log inclusion proof; the bundle file is
uploaded to the Release alongside its artefact:

```yaml
signs:
  - id: cosign
    artifacts: all
    cmd: cosign
    signature: "${artifact}.cosign.bundle"
    args:
      - sign-blob
      - --yes
      - --bundle=${signature}
      - ${artifact}
    output: true
```

The `--bundle` flag writes the modern sigstore-bundle format (single
JSON file); `cosign verify-blob --bundle <file>` is mutually exclusive
with the legacy `--signature` + `--certificate` flag pair per the
sigstore.dev docs. flux and recent argocd releases attach bundles by
the same shape.

> **ADR 0006 deviation — bundle vs. legacy two-file form.** ADR
> 0006's original G2.6 Implications block prescribed the legacy
> `--output-signature` + `--output-certificate` two-file form.
> The CLI release pipeline adopts the modern `--bundle` form
> instead — it's current sigstore best practice, what flux and
> recent argocd ship, and `cosign verify-blob --bundle` is
> mutually exclusive with the legacy flag pair so a single recipe
> covers all operators. The same evolution happened at
> `chart.yml` (PR #173) and `image.yml` (PR #165); a follow-up
> ADR amendment will record this across all three pipelines.

> **ADR 0006 deviation — per-workflow split vs. single release.yml.**
> ADR 0006 originally sketched a single `release.yml` covering
> image + chart + CLI. The implemented architecture splits these
> into three independent workflows (`image.yml`, `chart.yml`,
> `cli-release.yml`) because each artefact has a distinct trigger
> surface, permission set, and runner profile — putting them
> behind one workflow would make `permissions:` either
> over-broad or littered with per-step elevation. The per-workflow
> split is now the canonical pattern; the identity-claim regex
> shape stays uniform so operators learn one verification recipe.

The cosign-installer GitHub Action (`sigstore/cosign-installer@<sha>`,
pinned in `cli-release.yml` to the same v4.1.2 SHA `chart.yml` uses)
puts a cosign binary on PATH before the GoReleaser step. v4.x of the
installer dropped pre-2.0 cosign support; v3.x of cosign has
keyless-by-default semantics — no `COSIGN_EXPERIMENTAL=1` needed.

#### Identity claim (locked by ADR 0006)

The cert Fulcio issues binds to the workflow file path + ref of the
run that minted the OIDC token. Operators verify against:

```
^https://github\.com/evoila/meho/\.github/workflows/cli-release\.yml@refs/tags/v.+$
```

The anchor on `cli-release.yml` and the `refs/tags/v` prefix rejects
bundles produced by a fork's workflow or by a non-tag push. The same
regex shape (only the workflow basename changes) is used at
`chart.yml` (chart signing) and `image.yml` (image signing) per
ADR 0006 — operators have one identity-claim format to learn, three
artefact types to apply it to.

#### Two-step trust chain

`SHA256SUMS` is itself signed, which lets operators verify once and
trust a whole release worth of tarballs without re-running cosign
per file:

1. `cosign verify-blob --bundle SHA256SUMS.cosign.bundle SHA256SUMS`
2. `sha256sum -c SHA256SUMS` against whichever tarballs they
   actually downloaded.

The order matters — verifying the signature on `SHA256SUMS` first
proves the checksums come from the workflow identity; verifying
checksums after that proves the tarballs match what was signed.
Reversing the order would let an attacker swap tarballs without
breaking the (still-valid) signature on the original `SHA256SUMS`.

The full operator-side recipe lives at
[`cli/README.md`](../../cli/README.md#verify-signatures) and at the
top-level [`README.md`](../../README.md#verifying-cli-release-artefacts).

#### Snapshot builds skip signing

`make release-dry-run` shells `goreleaser release --snapshot --clean
--skip=publish,sign`. Per `goreleaser release --help`, `--snapshot`
alone implies only `--skip=announce,publish,validate` — it does NOT
skip the `signs:` block. We pass `--skip=sign` explicitly so the
dry-run completes on a dev machine without cosign on PATH (and
without the `id-token: write` permission that's only available in a
real CI run). Snapshot builds therefore produce only tarballs +
`SHA256SUMS` under `cli/dist/`; the `.cosign.bundle` files are a
tag-push-only artefact, produced by the CI workflow which omits
`--skip=sign`.

### Draft mode

`release: draft: true` creates the GitHub Release as a draft. A
maintainer flips it to public via the GitHub UI after verifying
the four tarballs + matching `.cosign.bundle` files are present
and `meho version` reports the expected tag. The conservative
posture stays for the first few public releases — once the full
pipeline (signing + verification + anonymous-pull) is proven end
to end and dogfooding catches any regressions, the draft flag
becomes a one-line edit.

`release: prerelease: auto` flips the GitHub "pre-release" flag
based on whether the tag contains a semver pre-release identifier
(per https://semver.org). `v0.1.0-rc.1` → pre-release;
`v0.1.0` → stable.

### Local dry-run

`make release-dry-run` runs `goreleaser release --snapshot --clean
--skip=publish,sign` against the local checkout. Snapshot mode
synthesises a `0.0.1-snapshot` version so the run works on any
branch without needing a real `v*` tag in git; `--skip=publish`
keeps the GitHub Release / Homebrew tap publishers off so an
operator can't accidentally push to upstream from their laptop; and
`--skip=sign` keeps the cosign `signs:` block from firing locally
(it requires cosign on PATH and `id-token: write` — neither
available outside CI). The output lands at `cli/dist/`, gitignored.

`make release-check` runs `goreleaser check` for config-only
validation — useful as a fast feedback loop when editing
`.goreleaser.yaml` without producing artefacts.

Both targets install GoReleaser into `cli/bin/` on first run
(pinned to v2.15.4 for developer reproducibility). The GHA workflow
uses `goreleaser/goreleaser-action@<sha>` with `version: '~> v2'`
so security and bug-fix releases land automatically — the v2 major
schema is what matters for stability, not the patch version.

### Reproducible-build limits

Within-tarball reproducibility is exact: the same tag produces
byte-identical binaries across rebuilds (`mod_timestamp` +
`CommitDate` ldflag + `-trimpath`). The gzip wrapper around each
tarball has its own embedded mtime that varies between runs — the
binary's content is identical, the gzip stream is not.

Cosign signs the gzip stream the workflow actually uploads (the
`signs:` block runs against the file on disk under `cli/dist/`),
not the inner binary. A second tag-push of the exact same tag
would therefore produce a different `.tar.gz` digest and a
non-matching signature — but signatures aren't compared between
runs; each is verified independently against the cert's Fulcio
identity claim and the Rekor inclusion proof. The reproducibility
that matters for the trust chain is the **binary**'s content (so
operators can re-build from source and verify nothing was tampered
with via `make build`); GoReleaser's gzip-stream non-determinism
doesn't undermine that.

## Dependencies

Direct:

* `github.com/spf13/cobra` — CLI framework, per ADR 0004.
* `github.com/zalando/go-keyring` — cross-platform OS keyring,
  chosen over `99designs/keyring` (which ADR 0004 rejected on
  maintenance-cadence grounds — last release December 2022).
* `golang.org/x/oauth2` — supplies `Config.DeviceAuth` and
  `Config.DeviceAccessToken` for the RFC 8628 device-code flow,
  plus `Config.TokenSource` for the T3 refresh path. Pinned at
  `v0.26.0`, the last release that still targets Go 1.22; later
  versions require Go 1.23+ and would bump the module's go
  directive prematurely.
* `github.com/oapi-codegen/runtime` — runtime helpers the generated
  client uses (JSON merging for `oneOf` unions, parameter styling
  per RFC 6570). Pinned at `v1.1.1` for Go 1.22 compatibility;
  later runtimes require Go 1.24+.

Build-time tool (not in `go.mod`; installed under `bin/` via
`make tools`):

* `github.com/oapi-codegen/oapi-codegen/v2` — the OpenAPI → Go
  client generator itself. Pinned at `v2.5.0`, the last v2.x
  release that still targets Go 1.22 as the minimum module go
  directive. `make tools` runs `go install …@v2.5.0` with
  `GOBIN=$PWD/bin`; the generator itself executes on a Go 1.24+
  toolchain that Go downloads automatically (the `go install`
  command honours the dep's `go` directive).

Indirect transitive deps tracked via `go mod tidy` in `go.sum`. The
project keeps the dep graph small — every transitive import is one
more thing supply-chain scanning has to vouch for, and operators
have to trust to run `meho login` against their secrets.

## Known issues / forward-compat scaffolding

* `meho version` prints CLI metadata only. The Goal #11 contract
  also calls for a backplane-version line; this is now feasible
  (the AuthedClient can call `GET /version`) but deferred until
  Initiative G2.7 wires its CI seam so the format choice doesn't
  thrash. Filed as a follow-up adjacent to T3.
* Persistent `--config` and `-v/--verbose` flags are registered on
  the root command but not yet consumed; reserved for v0.2.
* The auth-config endpoint at `/api/v1/auth-config` doesn't exist on
  the backplane yet (a G2.2 coordination Task). Operators using
  this Task's binary against today's backplane must pass `--issuer`
  and `--client-id` explicitly to `meho login`; the prose error
  message guides them to those flags.
* The `/api/v1/commands` discovery endpoint doesn't exist on the
  backplane yet (G2.2 coordination, identical to `/api/v1/auth-config`).
  The CLI's discovery fetch degrades to "no extra commands"
  silently until G2.2 lands the endpoint.
* No CI drift check on the OpenAPI snapshot. If a backend
  contributor adds a route without running `make snapshot-openapi`,
  the snapshot drifts out of sync silently. G2.7 will add a CI
  job that re-snapshots and diffs against the committed copy.
* The 401-refresh happy path isn't yet covered by a unit test —
  mocking Keycloak's well-known + token-exchange end to end is
  heavyweight, and the G2.8 integration suite covers it against a
  real Keycloak realm. The no-refresh-token branch (which surfaces
  `auth_expired` immediately) is unit-tested.
* Browser auto-launch (xdg-open / open) is deferred — v0.1 prints
  the URL and lets the operator copy-paste, matching how
  `gh auth login` behaves without `--web`.

## References

* Parent Goal: [#11](https://github.com/evoila-bosnia/meho-internal/issues/11)
* Parent Initiative: [G2.6 #42](https://github.com/evoila-bosnia/meho-internal/issues/42)
* Stack ADR (locked): [#13](https://github.com/evoila-bosnia/meho-internal/issues/13)
* Cosign keyless ADR (locked): [#15](https://github.com/evoila-bosnia/meho-internal/issues/15) — same identity-claim format used by image (`image.yml`) and chart (`chart.yml`) signing.
* cobra docs: https://github.com/spf13/cobra
* zalando/go-keyring: https://github.com/zalando/go-keyring
* golang.org/x/oauth2 device flow: https://pkg.go.dev/golang.org/x/oauth2#Config.DeviceAuth
* RFC 8628 — Device Authorization Grant: https://datatracker.ietf.org/doc/html/rfc8628
* golangci-lint config reference: https://golangci-lint.run/
* GoReleaser `signs:` block: https://goreleaser.com/customization/sign/
* cosign sign-blob: https://docs.sigstore.dev/cosign/signing/signing_with_blobs/
* cosign verify (incl. verify-blob): https://docs.sigstore.dev/cosign/verifying/verify/
* Empirical comparables for the scaffold + signed-release pattern:
  `gh` (GitHub CLI), `argocd`, `flux`. All use cobra +
  ldflags-injected version, all ship single static binaries, and
  flux + recent argocd releases attach sigstore bundles by the same
  shape this Task wires.
