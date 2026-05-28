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

The v0.2 substrate adds several statically-registered subcommand
trees alongside the discovery surface. All follow the same pattern:
a per-package `NewRootCmd()` returns a cobra parent that holds
per-verb subcommands; the parents register before the dynamic
discovery hook so backplane manifests cannot shadow built-in verb
names.

- `meho retrieval ...` (G4.3 #373) — retrieval-quality + migration-
  decision tooling. v0.2 ships `eval`, `usage`, and
  `retire-checklist`.
- `meho operation ...` (G0.6-T13 #481) — dispatcher meta-tools
  (`groups`, `search`, `call`).
- `meho connector ...` (G0.7-T5 #405) — spec-ingestion + review
  workflow (`ingest`, `list`, `review`, `edit-group`, `edit-op`,
  `enable`, `disable`).
- `meho audit ...` (G8.1-T3 #467) — audit-log query surface
  (`query`, `recent`, `show`, `who-touched`, `my-recent`) wrapping
  the four `/api/v1/audit/*` routes shipped by G8.1-T2 (#466). G8.2-T5
  (#1013) adds `replay <session-id>` — an ASCII parent/child session
  tree over `GET /api/v1/audit/sessions/{id}/replay` (`--json`,
  `--max-depth`; a 413 `session_too_large` redirects to
  `query --session-id`) — plus a `--session-id` filter on `query`.
- `meho kb ...` (G4.1-T4 #418) — knowledge-base operator surface
  (`ingest`, `search`, `list`, `show`, `add`, `delete`) wrapping
  the five `/api/v1/kb*` routes shipped by G4.1-T2 (#416) plus the
  `/api/v1/retrieve` route (G0.4-T5 #262, `source="kb"` scoped)
  for the search verb.
- `meho conventions ...` (G7.1-T3 #315) — tenant-conventions
  operator surface (`list`, `show`, `create`, `edit`, `delete`,
  `history`) wrapping the six `/api/v1/conventions*` routes
  shipped by G7.1-T2 (#314). `edit` ships in two modes: flag-driven
  PATCH (scripting path) and `$EDITOR` interactive (operator
  conversational-edit path; fetches current body, opens
  $EDITOR/$VISUAL/vi on a `.md` tempfile, submits saved content as
  a `body`-only PATCH). `history` renders unified-diff per row
  (body_before → body_after); `--json` exposes raw history rows
  for `jq`/`diff -u` pipelines. The dropped-slug warning the issue
  body expected on `list` lives behind a T4 API surface that hasn't
  shipped yet; the verb is structurally ready.
- `meho remember / recall / forget / list` (G5.1-T4 #424) — memory
  operator surface, registered as **top-level** verbs (no `memory`
  parent — per consumer-needs.md §G5's ergonomic shape:
  `meho remember "note"` rather than `meho memory remember "note"`).
  Wraps the four `/api/v1/memory*` routes shipped by G5.1-T2 (#422)
  plus the `/api/v1/retrieve` route (G0.4-T5 #262, `source="memory"`
  scoped) for `meho recall --query`. Five scopes: `user` /
  `user-tenant` / `user-target` / `tenant` / `target`. The two
  target-scoped values require `--target NAME`; the CLI rejects a
  missing `--target` client-side before the round-trip.
- `meho migrate ...` (G5.3 #608–#612) — laptop-local memory migration
  surface. T1 (#608) ships the `migrate` parent + `memory` subcommand
  skeleton. T2 (#609) adds the frontmatter scanner + scope-suggestion
  table. T3 (#610) adds the machine-local detector. T4 (#611) wires the
  interactive `huh` picker, `--dry-run` (JSON envelopes), and
  `--non-interactive` (user/feedback only) paths. T5 (#612) adds the
  real HTTP submission layer (POST `/api/v1/memory`), post-login nudge,
  marker file, and `docs/cli/memory-migration.md`. Depends on
  `charm.land/huh/v2` (MIT).

## Module layout

```text
cli/
├── go.mod                  # github.com/evoila/meho/cli; Go 1.25.8.
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
    │   ├── login.go           # `meho login` subcommand + auth-config discovery + config persistence + post-login memory-migration nudge (T5 #612).
    │   ├── login_test.go      # override-resolution + help-flag + post-login nudge tests.
    │   ├── status.go          # `meho status` subcommand + --json + URL resolver.
    │   ├── status_test.go     # happy/JSON/no-creds/unreachable/401/redaction tests.
    │   ├── audit/            # G8.1-T3 #467 — `meho audit …` verb tree.
    │   │   ├── audit.go          # NewRootCmd + shared HTTP/auth helpers.
    │   │   ├── query.go          # `meho audit query` (POST /api/v1/audit/query).
    │   │   ├── recent.go         # `meho audit recent` — shortcut for `query --since 24h`.
    │   │   ├── show.go           # `meho audit show <audit-id>` (GET /api/v1/audit/show/{id}).
    │   │   ├── who_touched.go    # `meho audit who-touched <target>` (GET /api/v1/audit/who-touched/{target}).
    │   │   ├── my_recent.go      # `meho audit my-recent` (GET /api/v1/audit/my-recent).
    │   │   ├── replay.go         # `meho audit replay <session-id>` (GET /api/v1/audit/sessions/{id}/replay) — ASCII tree + 413 redirect.
    │   │   ├── audit_test.go     # helper + URL-normalisation + register-all-verbs tests.
    │   │   ├── query_test.go     # body-marshal + render + 400-passthrough + --session-id tests.
    │   │   ├── show_test.go      # path-escape + 404 / 422 surface + summary render tests.
    │   │   ├── replay_test.go    # tree render + --json + --max-depth fold + 413 redirect tests.
    │   │   ├── who_touched_test.go # query-param emit + table render tests.
    │   │   ├── my_recent_test.go # JWT-only-principal contract tests.
    │   │   └── recent_test.go    # since=24h binding + --json passthrough tests.
    │   ├── kb/               # G4.1-T4 #418 — `meho kb …` verb tree.
    │   │   ├── kb.go             # NewRootCmd + newAuthedClient / retryOn401 / renderHTTPStatus typed-client helpers (G0.12-T9 #1267) + body/metadata/confirm helpers.
    │   │   ├── ingest.go         # `meho kb ingest <directory> [--dry-run]` (POST /api/v1/kb/ingest).
    │   │   ├── search.go         # `meho kb search <query>` (POST /api/v1/retrieve, source="kb").
    │   │   ├── list.go           # `meho kb list [--filter --limit --offset]` (GET /api/v1/kb).
    │   │   ├── show.go           # `meho kb show <slug>` (GET /api/v1/kb/{slug}); body to stdout.
    │   │   ├── add.go            # `meho kb add <slug> --body @file|@-|text` (POST /api/v1/kb).
    │   │   ├── delete.go         # `meho kb delete <slug> [--confirm]` (DELETE /api/v1/kb/{slug}).
    │   │   ├── kb_test.go        # helpers + register-all-verbs + body/metadata/confirm contract tests.
    │   │   ├── ingest_test.go    # POST body + four-bucket render + 400 directory_not_found tests.
    │   │   ├── search_test.go    # POST body (source pinned) + table render + nil-score safety tests.
    │   │   ├── list_test.go      # query-param emit + table render + limit-range gate tests.
    │   │   ├── show_test.go      # path-escape + Markdown body to stdout + 404 slug_not_found tests.
    │   │   ├── add_test.go       # body-from-file / @- / inline + metadata parse + 422 surface tests.
    │   │   └── delete_test.go    # confirm-prompt + idempotent-204 + --json envelope tests.
    │   ├── conventions/      # G7.1-T3 #315 — `meho conventions …` verb tree.
    │   │   ├── conventions.go    # NewRootCmd + shared HTTP/auth helpers + body/confirm helpers + $EDITOR seam (runEditor var).
    │   │   ├── list.go           # `meho conventions list [--kind K]` (GET /api/v1/conventions); table or --json.
    │   │   ├── show.go           # `meho conventions show <slug>` (GET /api/v1/conventions/{slug}); Markdown body to stdout.
    │   │   ├── create.go         # `meho conventions create --slug --kind --title --body @file [--priority]` (POST /api/v1/conventions).
    │   │   ├── edit.go           # `meho conventions edit <slug>` (PATCH /api/v1/conventions/{slug}); flag-driven OR $EDITOR interactive.
    │   │   ├── delete.go         # `meho conventions delete <slug> [--confirm]` (DELETE /api/v1/conventions/{slug}).
    │   │   ├── history.go        # `meho conventions history <slug> [--limit N]` (GET /api/v1/conventions/{slug}/history); unified-diff per row.
    │   │   ├── conventions_test.go # helpers + register-all-six-verbs + body/confirm/path-escape contract tests.
    │   │   └── crud_test.go      # per-verb HTTP-server tests: list table + JSON, show 404, create 409/422-over-budget, edit flag/$EDITOR modes + 422 inline surface, delete confirm/decline/404, history diffs + --limit + --json.
    │   ├── memory/           # G5.1-T4 #424 — top-level `meho remember/recall/forget/list/promote` (no parent).
    │   │   ├── memory.go         # Scope alias for api.MemoryScope + newAuthedClient/retryOn401/renderHTTPStatus typed-client helpers (G0.12-T10 #1268) + parseScope/parseTTL/parseTags/parseScopeSlugArg/loadBody/confirmPrompt.
    │   │   ├── remember.go       # `meho remember <body> [--scope --slug --target --tag --ttl --persist --json]` (POST /api/v1/memory). `--persist` (G5.2-T2 #624) sends explicit `expires_at: null` to opt out of the backend's default-7-day TTL on `memory-user` writes.
    │   │   ├── recall.go         # `meho recall <scope>/<slug>` or `meho recall --query` (GET /api/v1/memory/{scope}/{slug} or POST /api/v1/retrieve, source="memory").
    │   │   ├── forget.go         # `meho forget <scope>/<slug> [--confirm --target --json]` (DELETE /api/v1/memory/{scope}/{slug}).
    │   │   ├── list.go           # `meho list [--scope --tag --slug-pattern --include-expired --limit --json]` (GET /api/v1/memory).
    │   │   └── memory_test.go    # parseScope/parseTTL/parseScopeSlugArg + verb-happy-path + 403/404/422 + decline + JSON envelope tests.
    │   ├── connector/         # G0.7-T5 #405 — `meho connector …` verb tree. G0.12-T7 #1265 migrated every verb onto the generated typed client (api.ClientWithResponses via api.AuthedClient + retryOn401); api.CatalogListResponse / api.ConnectorReviewPayload / api.IngestRequest / api.IngestResponse / api.EditGroupBody / api.EditOpBody are the single source of truth, no consumer-side struct duplicates.
    │   │   ├── connector.go      # NewRootCmd + newAuthedClient / retryOn401 / renderRequestError / renderHTTPStatus helpers.
    │   │   ├── ingest.go         # `meho connector ingest` (POST /api/v1/connectors/ingest).
    │   │   ├── list.go           # `meho connector list` (GET  /api/v1/connectors). List endpoint returns dict[str, list[dict]] (no response_model on the backend; per-row UUID serialisation), so a package-private listEntry decode lives here.
    │   │   ├── review.go         # `meho connector review <id>` (GET  /api/v1/connectors/{id}/review).
    │   │   ├── edit_group.go     # `meho connector edit-group <id> <key>` (PATCH groups/{key}).
    │   │   ├── edit_op.go        # `meho connector edit-op <id> <op>` (PATCH operations/{op}).
    │   │   ├── enable.go         # `meho connector enable <id>`  + shared transition factory + `disable`.
    │   │   ├── disable.go        # `meho connector disable <id>` (constructor only; logic in enable.go).
    │   │   └── connector_test.go # pure-function + typed-client mocked HTTP contract tests.
    │   ├── operation/         # G0.6-T13 #481 — `meho operation …` meta-tool surface.
    │   │   ├── operation.go      # NewRootCmd + operationsAPI seam + apiResponseError sentinel + loadParamsFlag (G0.12-T2 #1260).
    │   │   ├── groups.go         # `meho operation groups` (GET /api/v1/operations/groups) — typed via api.GetGroupsApiV1OperationsGroupsGetParams.
    │   │   ├── search.go         # `meho operation search` (GET /api/v1/operations/search) — typed via api.GetSearchApiV1OperationsSearchGetParams.
    │   │   ├── call.go           # `meho operation call`   (POST /api/v1/operations/call) — typed via api.CallOperationBody + FromCallOperationBodyTarget0.
    │   │   ├── operation_test.go # render + helper + sentinel tests.
    │   │   └── client_test.go    # G0.12-T2 #1260 — fakeOperationsClient mocks the operationsAPI seam; asserts typed request params + 401 refresh dance + error classification.
    │   ├── retrieval/         # G4.3-T2 #441 — retrieval-quality tooling. G0.12-T12 #1270 moved every verb onto the generated `api.ClientWithResponses` (no consumer-side struct copies).
    │   │   ├── retrieval.go            # NewRootCmd + newAuthedClient + retryOn401 + renderRequestError/renderHTTPStatus + 1 MiB capRoundTripper.
    │   │   ├── retrieval_test.go       # renderer + 401-retry + oversized-response (M1) + nil-payload guard (M2-M6 pre-empt).
    │   │   ├── eval.go                 # `meho retrieval eval` (POST /api/v1/retrieve/eval) via typed client.
    │   │   ├── eval_test.go            # output-contract + URL-resolution + EvalRequest body shape tests.
    │   │   ├── usage.go                # `meho retrieval usage` (GET /api/v1/retrieve/usage) — G4.3-T5b #464, typed client.
    │   │   ├── usage_test.go           # query-param + wire-shape + 403/400 routing + JSON200 nil-guard tests.
    │   │   ├── retire_checklist.go     # `meho retrieval retire-checklist` (POST /api/v1/retrieve/retire-checklist) — G4.3-T6 #445, typed client. Keeps the hand-typed `ghIssueLabel` / `ghIssue` for the `gh issue list` subprocess output.
    │   │   └── retire_checklist_test.go # surface-bucket + table-render + body-shape (null vs empty) tests.
    │   ├── migrate/           # G5.3 #608–#612 — `meho migrate …` laptop-local migration verb tree (Initiative #375). G0.12-T11 #1269 migrated to typed client.
    │   │   ├── migrate.go        # NewRootCmd + _ import charm.land/huh/v2.
    │   │   ├── memory.go         # `meho migrate memory` RunE — interactive picker / --dry-run / --non-interactive. Dry-run envelope is `api.RememberBody` directly (post-T11 #1269; no consumer-side dryRunEnvelope shadow).
    │   │   ├── memory_test.go    # --dry-run envelope, --non-interactive filter, machine-local skip, empty-dir guard, wire-body stability.
    │   │   ├── submit.go         # doSubmit + spinner + RememberApiV1MemoryPostWithResponse via api.AuthedClient + retryOn401 generic. G0.12-T11 #1269 dropped the in-package HTTP helper trio (doAuthedRequest/sendRequest/httpError) + the local `source_id`-in-body bug the typed RememberBody schema's `extra="forbid"` would have rejected on a real backend (httptest mock masked it). isTransient retry logic preserved.
    │   │   └── submit_test.go    # typed RememberBody body shape, same-slug rerun stable, no-source_id-on-wire, transient retry, summary line, --mark-migrated, 201-without-payload nil-guard, 401/403/422 classification, no-backplane → auth_expired.
    │   ├── vmware/            # G3.1-T7 #511 — `meho vmware …` alias verb tree (connector_id="vmware-rest-9.0" pre-baked).
    │   ├── vault/             # G3.3-T6 #550 — `meho vault …` alias verb tree (connector_id="vault-1.x" pre-baked).
    │   └── topology/          # G9.1-T6 #454 + G9.2-T6 #599 — `meho topology refresh/dependents/dependencies/path/annotate/unannotate/list-edges` over the T5 REST surface (#453, #597).
    │                          #   (the 5th G9.1-T6 verb, `meho targets discover`, lives in targets/discover.go.)
    │       ├── vault.go          # NewRootCmd + shared HTTP/auth/render helpers + ConnectorID const.
    │       ├── dispatch.go       # CallResult/callRequestBody + dispatchOp + renderCallResult + generic renderer.
    │       ├── kv.go             # `meho vault kv read|list|put|versions|delete` (vault.kv.* ops, #545).
    │       ├── sys.go            # `meho vault sys health|seal-status|mounts-list|auth-list` (vault.sys.* ops, #546).
    │       ├── auth.go           # `meho vault auth userpass/approle list+read` (vault.auth.* ops, #547).
    │       └── vault_test.go     # helpers + verb-tree wiring + flag→params wire-shape + e2e mocked-backplane tests.
    ├── migrate/               # G5.3 — pure-logic helpers for the memory migration flow (Initiative #375).
    │   ├── doc.go                # package doc.
    │   ├── machinelocal.go       # DetectMachineLocal — heuristic detector for laptop-local content (#610).
    │   ├── machinelocal_test.go  # table-driven per-Category tests + truncation + seam coverage (#610).
    │   ├── marker.go             # G5.3-T5 #612 — TouchMarker / MarkerExists — XDG migration-complete marker file; full implementation.
    │   ├── marker_test.go        # touch + exists + idempotent + delete-re-enables + sanitizeDirName.
    │   ├── picker.go             # G5.3-T4 #611 — BuildForm (huh), SubmitPlan, FinalizeSkip, DefaultPlan, slugFromPath, SourceIDPrefix, scope/action builders.
    │   ├── picker_test.go        # slug, validateSlug, BuildForm structure, role-filtered scope options, FinalizeSkip, DefaultPlan.
    │   ├── scan.go               # G5.3-T2 #609 — ResolveSourceDir + ScanDir + MemoryFile (frontmatter parser + BodySHA256 + MachineLocalOptOut).
    │   ├── scan_test.go          # table-driven: well-formed/missing/malformed frontmatter, machine-local comment, BodySHA256 stability, ScanDir, ResolveSourceDir.
    │   ├── suggest.go            # G5.3-T2 #609 — SuggestScope table + exported Scope* constants.
    │   └── suggest_test.go       # full mapping table including tenantConfigured branch and unknown-type fallback.
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
   realm issuer and the public device-code `client_id` to use. The
   response shape is
   `{"keycloak_issuer": "...", "audience": "...", "cli_client_id": "..."}`.
   * **Field mapping.** `keycloak_issuer` drives the OIDC issuer
     URL; `cli_client_id` drives the OAuth `client_id` for the
     device-code grant. `audience` is the **confidential**
     resource-server identifier the backplane validates inbound
     JWTs against — it is NOT used as `client_id` (Keycloak rejects
     device-code initiation against a confidential client with
     `401 unauthorized_client`). v0.3.1 shipped without
     `cli_client_id` and the CLI mis-mapped `audience` → `client_id`,
     which deadlocked `meho login` on its documented happy path
     (G0.9.1-T9, RDC Signal #16, 2026-05-21); v0.3.2 added the
     dedicated field and fixed the mapping.
   * **Absent / empty `cli_client_id`.** When the field is missing
     (older backplane) or empty (newer backplane without
     `KEYCLOAK_CLI_CLIENT_ID` wired), the CLI surfaces an
     actionable error naming the public-client requirement and the
     `--client-id` override rather than silently retrying with
     `audience`.
   * **Operator override.** Pass `--issuer` and `--client-id` to
     skip discovery entirely — useful when the backplane URL isn't
     reachable on the operator's network but the IdP is. A partial
     override (just one flag) still hits the backplane for the
     other half. TLS-discovery failures additionally point at the
     "install your deployment's root CA in your system trust store"
     remediation for internal-CA deployments.
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
   expires (`expires_in`, enforced inside the oauth2 package), the
   operator denies the grant (`access_denied`), or the polling
   context is cancelled. The outer timeout is 10 minutes
   (`auth.PollTimeout`).

   **Detached from ambient deadlines.** The interactive approval
   wait runs on a context built by `auth.NewDeviceFlowContext`,
   which deliberately drops the ambient `cmd.Context()` deadline
   while keeping context values (`oauth2.HTTPClient` etc.) and
   re-attaching `SIGINT` / `SIGTERM` cancellation. Rationale:
   non-interactive wrappers (CI steps, the Claude Code bash tool,
   `timeout 30s …` prefixes) often impose deadlines shorter than
   the device code's `expires_in`. Without the detachment, a
   wrapper-imposed deadline would propagate into the polling loop
   and surface as `context deadline exceeded` even though the
   device code was still valid (Initiative G0.9.1, Wall #4). Only
   the interactive wait detaches — the discovery and auth-config
   HTTP calls (steps 1–2) still honour `cmd.Context()`, so genuine
   network wedges fail fast.

   When the wait does time out, `classifyDeviceTokenError`
   distinguishes the culprit:

   * IdP-reported `expired_token` / `access_denied` → device-code-
     specific messages.
   * `PollTimeout` (10m) elapsed and the ambient parent context is
     ALSO past deadline → message names the wrapping timeout as
     the cause and points the operator at running outside the
     wrapper.
   * `PollTimeout` elapsed with a healthy parent → message says
     the operator didn't approve in 10 minutes.
   * `context.Canceled` → SIGINT / SIGTERM message.
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
unconditionally. It is also surfaced in `meho login --help` so an
operator grepping the help output for "keyring" finds it without
reading the source.

### Auto-fallback on runtime size errors

macOS Keychain's legacy `kSecValueData` path caps a single value at
~4 KiB, and go-keyring's `add-generic-password` shell-out enforces
a hard 4096-byte command-line limit. A full OIDC token bundle
(access_token + refresh_token + id_token, JSON-wrapped, plus the
library's `go-keyring-base64:` chunk marker) regularly exceeds that
cap and surfaces as `keyring.ErrSetDataTooBig`. To keep `meho login`
working on macOS out of the box, `NewTokenStore` returns a
`fallbackStore` decorator that wraps the keyring backend with the
file backend as a secondary. On `Save`, if the keyring rejects the
payload with that specific sentinel (matched via `errors.Is`, not a
brittle string), the wrapper transparently writes to the file
backend and remembers that fact so `Describe()` — which the success
message prints — names the credentials file the operator can
actually inspect. Every other keyring failure (locked Keychain,
D-Bus unreachable, Wincred ACL denial) is left to surface unchanged
so unrelated outages don't silently route tokens to disk.

`Load` bridges to the secondary only on `ErrTokenNotFound` from the
primary — the case where a previous invocation hit the size-fallback
path on `Save` and the token sits in the file store. AC #1 ("a
subsequent `meho status` reads the bearer") would regress without
this bridge, because a fresh `fallbackStore` in the next process
starts with the primary reporting "no entry" for that
`(service, user)`. Every other primary error (locked Keychain, D-Bus
unreachable, malformed entry) propagates unchanged, so a real
keyring outage still surfaces as an error instead of masking it with
a stale file-store entry.

`Delete` goes to the primary store only. After a size-triggered
fallback the secondary still holds the token, and an operator who
needs to scrub the credentials file by hand is expected to do so
explicitly — re-running `meho login` overwrites it in the normal
case.

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

## Watch flow (`meho status --watch`, G6.1-T5)

`--watch` flips `meho status` from a one-shot health probe to a
long-lived SSE subscriber on the backplane's `/api/v1/feed` endpoint.
The renderer streams one line per broadcast event until the
operator hits Ctrl-C; filters (`--op-class`, `--principal`,
`--target`) forward to the SSE query string, and disconnects
retry with exponential backoff using `Last-Event-Id` for replay.

### Dispatch

`newStatusCmd`'s `RunE` checks the `--watch` flag and dispatches:

- `false` → `runOneShot` (the original `GET /api/v1/health` flow,
  extracted from the inline closure when T5 added the second arm).
- `true` → `runWatch` (the SSE subscriber in `status_watch.go`).

Both arms share the same bearer-token resolution path and the same
`--backplane` override, so the operator's expectation of "the URL
in `meho login`" stays consistent.

### SSE wire format

The backplane (`backend/src/meho_backplane/api/v1/feed.py`, G6.1-T4)
emits frames in the standard WHATWG `EventSource` shape:

```
event: broadcast
data: {"event_id":"...", "ts":"...", "principal_sub":"...", ...}
id: 1715600000000-0
<blank line>
```

Heartbeats (`: heartbeat\n\n`) keep the connection alive across
nginx/ALB idle timeouts and are dropped silently by the parser
(SSE comment lines, not events). Multi-line `data:` fields are
joined with `\n` before JSON parsing.

### Reconnect / backoff

`runWatch` retries on every recoverable failure (transport error,
unexpected EOF, scanner error) using the schedule from the T5 issue
body: **1s, 2s, 5s, 10s, 30s, then 30s indefinitely**. Each retry
carries `Last-Event-Id: <id>` with the last successfully-rendered
event id, so the backplane replays events the operator missed
during the gap (T4's iter-3 cursor-validation gate enforces the
ID shape — malformed ids return 400 and break the loop).

Non-recoverable HTTP responses (401 / 403 / 400 / other 5xx) do
NOT retry: the operator has to take action (`meho login` for 401,
ask for an operator-role grant for 403, file a bug for unexpected
status codes). Each surfaces via `output.RenderError` with its
own structured code:

- **401** → `auth_expired` (exit 2). Same code as the one-shot
  status path so the operator's mental model stays consistent.
- **403** → `insufficient_role` (exit 5). New code added in T5
  because re-running `meho login` won't help — the remedy is a
  tenant-admin role grant.
- **400** → `unexpected_response` (exit 4) with the body's detail
  string. The only realistic 400 today is an invalid SSE cursor
  the operator hand-edited in a wrapper script.

### Output discipline

Same Goal #11 §5 split as one-shot status:

- **Default human path**: one space-padded line per event
  (`<ts>  <principal>  <op_id>  <result_status>  <summary>`).
  Summary is `(aggregate-only)` for `credential_read` and
  `audit_query` op classes; otherwise `target=<name>` when the
  event carries a target; otherwise empty.
- **`--json`**: one raw JSON document per line — the SSE `data:`
  field byte-identical, with one trailing newline. `meho status
  --watch --json | jq` is the canonical agent-consumer shape.
- **Errors**: `RenderError` envelope to stderr; stdout stays clean
  on the JSON path so a consumer's `jq` doesn't choke on a
  half-event followed by an error blob.

The bearer token never reaches stdout/stderr — same `eyJ`-prefix
redaction stance applies, and the unit tests pin the marker.

### Test architecture

`status_watch_test.go` drives end-to-end coverage:

- An in-process `fakeFeed` httptest server records every received
  Authorization, Last-Event-Id, and query string and serves
  scripted SSE bodies. Frames are written ONCE across all
  connections combined so the reconnect-replay path doesn't loop
  forever (a naive "each connection writes all frames" model
  busy-loops because the cursor never advances past the same
  batch). The handler holds the connection open via
  `<-r.Context().Done()` after the scripted frames so the client's
  scanner sits in `Scan()` until the test cancels.
- Tests that assert on recorded requests run `runWatch` in a
  background goroutine and synchronise on `fakeFeed.waitForRequests`
  (block until N requests have landed, bounded by a generous
  timeout) before cancelling — never on a fixed `time.Sleep`. Gating
  on the observable event instead of wall-clock scheduling is what
  keeps the Go job green on slow CI runners; the joined `done`
  channel also gives the happens-before that lets the assertions read
  the captured `stdout`/`stderr` without racing the writer.
- A `fastBackoff` schedule (five 1 ms slots) collapses the
  production 1/2/5/10/30 s schedule so the suite runs in
  milliseconds.
- A `seedWatchCreds` helper writes a token + config to a
  `t.TempDir`-backed XDG home, mirroring the one-shot status
  tests' file-store discipline.

Body-shaping tests (parser, formatter, summariser, URL builder)
drive the pure helpers directly with table-driven cases; only the
end-to-end reconnect / 401 / 403 / Ctrl-C tests need the
`fakeFeed` server.

## Generated client (`internal/api/`)

`internal/api/client.gen.go` is produced by
[oapi-codegen v2.5](https://github.com/oapi-codegen/oapi-codegen)
from `cli/api/openapi.json` — a committed snapshot of the
backplane's OpenAPI document. v2.5 is the last v2.x release with
Go 1.25.8 minimum (raised from 1.22 by charm.land/huh/v2 v2.0.3's
transitive deps in PR #640). The generator itself runs
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

## Operation dispatch (`meho operation`, G0.6-T13 #481)

`cli/internal/cmd/operation/` registers the three cobra verbs that
wrap the G0.6 substrate's operation meta-tool surface (G0.6-T8 #399).
The verbs are operator-side parity for the agent-facing MCP tools
(`list_operation_groups`, `search_operations`, `call_operation`); the
agent and the operator hit the same dispatcher path. The earlier v1
chassis route `POST /api/v1/connectors/{product}/{op_id}` from G0.2-T6
(#245) was deprecated and removed by G0.6-T11 (#412) — two parallel
dispatch surfaces violated [CLAUDE.md](../../CLAUDE.md) postulate 5's
narrow-waist contract.

### Subcommands

- `meho operation groups <connector_id>` — calls
  `GET /api/v1/operations/groups`. Lists the enabled
  `OperationGroupSummary` rows for the connector with `operation_count`
  per group + a `when_to_use` blurb the agent consults to pick a group
  to search within. Unknown `connector_id` returns an empty `groups`
  list (operationally meaningful, never 404).
- `meho operation search <connector_id> <query> [--group K] [--limit N]`
  — calls `GET /api/v1/operations/search`. Runs hybrid BM25 + cosine
  RRF over `endpoint_descriptor` rows scoped to the connector
  (optionally narrowed to one `group_key`) and renders the top hits
  with `fused_score`. `--limit` is clamped by the API at 50.
- `meho operation call <connector_id> <op_id> --target <slug> [--params ...]`
  — calls `POST /api/v1/operations/call`. Invokes the G0.6 dispatcher
  end-to-end (parameter validation, policy gate, audit, JSONFlux,
  broadcast). The dispatcher always returns a structured
  `OperationResult` envelope; HTTP 200 carries both `status="ok"` and
  `status="error"` outcomes. The verb exits 1 on a non-ok envelope so
  shell pipelines see the gate-failed signal.

### Reserved flags (same shape across all three verbs)

- `--json` — emit the raw JSON envelope to stdout instead of the human
  render. Useful for piping into `jq` or capturing for diff.
- `--backplane <url>` — override the backplane URL (defaults to the URL
  recorded by `meho login`).
- `--params '<json>'` / `--params @<file>` (call only) — operation
  params. Inline JSON object or `@`-prefixed file path. The empty case
  (`--params` omitted) sends no `params` key on the wire — typed
  handlers that don't read params see an empty mapping at the
  validation layer.

### HTTP shape

All three verbs route through `api.NewAuthedClient(...)` and call the
generated typed client directly (G0.12-T2 #1260 — Initiative #1118
CLI hygiene migration). Per-verb request helpers (`getGroups`,
`getSearch`, `postCall`) build the typed params/body structs
(`api.GetGroupsApiV1OperationsGroupsGetParams`,
`api.GetSearchApiV1OperationsSearchGetParams`,
`api.CallOperationBody`), invoke the `*WithResponse` methods on the
embedded `*api.ClientWithResponses`, run a one-shot 401-refresh dance
on the `*api.AuthedClient.Refresh` hook (mirroring
`AuthedClient.GetHealth`), and parse the 200 body into the
hand-written response struct. Non-2xx outcomes wrap as the local
`*apiResponseError` sentinel that `renderRequestError` extracts
(`errors.As`) to pick the right `output.RenderError` category
(401→`auth_expired`, other non-2xx→`unexpected_response`, transport
failures→`unreachable`).

Response models stay hand-typed (`GroupSummary` + `GroupsResponse`,
`SearchHit` + `SearchResponse`, `CallResult`) because the FastAPI
surface types these routes' responses as `dict[str, Any]`; the
generator therefore emits the response as
`*map[string]interface{}`, which doesn't expose the typed
`OperationGroupSummary` / `OperationSearchHit` / `OperationResult`
shapes the renderer needs. Promoting the FastAPI return to a typed
model so the generator picks it up is a separate backend Task
explicitly out of scope for the consumer-side Initiative #1118.

For `call`, the `target` field uses the bare-string oneOf shape
via `api.CallOperationBody_Target.FromCallOperationBodyTarget0`
(G0.13-T2 #1132 — the forward-preferred form that round-trips
through the `query_topology` / `query_audit` read surfaces). The
CLI never emits the dict shape — the `fqdn` override is an MCP-
handler use case, not an operator-CLI use case. When `--target` is
omitted, `body.Target = nil` so the wire emits `"target": null`,
which the dispatcher accepts for typed handlers that resolve their
own context.

The package-local `operationsAPI` interface in `operation.go`
defines the minimal slice of `api.ClientWithResponsesInterface` the
three verbs consume (three `*WithResponse` methods + `Refresh`) so
`client_test.go` can substitute a tiny `fakeOperationsClient`
without reaching for the full ~140-method generated interface.
`*api.AuthedClient` satisfies the seam directly: it embeds
`*ClientWithResponses` (which provides the three `*WithResponse`
calls) and defines `Refresh` of its own. The test fake records the
typed params/body each verb passes and pops canned responses from
per-verb queues to drive the 401-dance and error-classification
scenarios.

### Exit codes

- `0` — verb ran cleanly; for `call`, `status == "ok"`.
- `1` — `call` only: dispatcher returned `status == "error"` or
  `status == "denied"` (connector raised, schema validation rejected,
  or policy denied — the three structured-failure envelopes the
  backend `Connector.execute` contract defines). Surfaced via the
  `errOpError` sentinel.
- `2` — `auth_expired` (no stored credentials, or refresh failed).
- `3` — `unreachable` (network / transport failure).
- `4` — `unexpected_response` (parse error, malformed JSON, etc.).

### MCP parity

The same three handlers also back the MCP tools registered in
[backend/src/meho_backplane/mcp/tools/operations.py](../../backend/src/meho_backplane/mcp/tools/operations.py)
(`list_operation_groups`, `search_operations`, `call_operation`).
Agents call the MCP tools; operators call the CLI verbs; both hit
the same backend functions in
[backend/src/meho_backplane/operations/meta_tools.py](../../backend/src/meho_backplane/operations/meta_tools.py).
The fourth route `GET /api/v1/operations/{descriptor_id}` (tenant-
admin diagnostic for `llm_instructions` inspection) is deferred — the
G0.6-T13 DoD was "three CLI verbs", not four.

## Targets registry (`meho targets`, G0.3-T5 #256)

`cli/internal/cmd/targets/` registers the operator-facing verbs that
wrap the targets registry routes from G0.3-T3 (#254), the G0.3-T1.5
(#477) probe-persistence remediation, and the G9.1-T6 (#454) discover
verb. The verbs are the operator-side surface for the per-tenant
`targets` table — a fingerprinted catalog of vendor systems the
operator manages (vCenter hosts, Vault instances, k8s clusters, …)
that the G0.6 dispatcher resolves at `call` time. Write verbs
(`create` / `update` / `delete`) are deferred; bulk import lands
under G0.3-T6 (#257).

### Subcommands

- `meho targets list [--product P] [--limit N] [--cursor C]` — calls
  `GET /api/v1/targets`. Renders the operator's tenant-scoped targets
  as a `NAME / ALIASES / PRODUCT / HOST` table. Results are keyset-
  paginated by name; `--cursor <last-name-seen>` walks pages. `--limit`
  is capped at 500 by the API; the CLI fails fast at the boundary so
  operators see the constraint without a 422 round-trip.
- `meho targets describe <name-or-alias>` — calls
  `GET /api/v1/targets/{name}`. Renders the full `Target` read shape
  as a stable key-value summary including the post-#477 fields
  `fingerprint` (cached `FingerprintResult` from the last successful
  probe) and `preferred_impl_id` (operator override for the G0.6
  resolver's tie-break ladder). Alias resolution happens server-side
  via `resolve_target`; a 404 surfaces the resolver's near-miss list
  so operators can correct a typo in one shot.
- `meho targets probe <name-or-alias>` — calls
  `POST /api/v1/targets/{name}/probe`. Backend invokes the registered
  `Connector.fingerprint(target)`, persists the `FingerprintResult` to
  `targets.fingerprint` (so the G0.6 resolver reads it without
  re-probing), and returns the envelope. On 501 (no connector
  registered for the target's product yet) the CLI appends a pointer
  to Goal G3 (per-product connectors) so operators know where the
  work tracks; the DB row is **not** touched and any previously-
  cached fingerprint survives. A connector that raises propagates as
  a 500; per the #477 accepted trade-off the CLI surfaces the
  underlying detail rather than masking it as a graceful failure.
- `meho targets discover <product> [--seed-target <name>]` — calls
  `GET /api/v1/targets/discover` (G9.1-T6 #454, the verb #256
  explicitly deferred here). Iterates every connector registered for
  `<product>`, calling each connector's `list_candidates` hook, and
  renders the merged candidate `NAME / HOST / PORT / CONFIDENCE`
  table plus a `SKIPPED / REASON` table for connectors that
  contributed nothing. Read-only — it never creates `targets` rows;
  the operator reviews and runs `meho targets create`
  (auto-registration is v0.2.next). `--seed-target` scopes discovery
  to one already-registered target's reach; it is resolved
  tenant-scoped server-side, so a cross-tenant seed name 404s like a
  typo. Documented in depth under "Topology verbs" (the verb is part
  of G9.1-T6 and shares that initiative's contract).

### Reserved flags (same shape across the verbs)

- `--json` — emit the raw JSON envelope to stdout instead of the human
  render. Stable schemas: `list` → `[]TargetSummary`; `describe` →
  full `Target` (including `fingerprint` + `preferred_impl_id`);
  `probe` → `FingerprintResult`; `discover` →
  `DiscoverResult` (`discovered` + `skipped`).
- `--backplane <url>` — override the backplane URL (defaults to the
  URL recorded by `meho login`).

### HTTP shape + error envelopes

All three verbs route through `api.NewAuthedClient(...)` for bearer
injection + one-shot 401-refresh-retry, mirroring the
`meho operation` sibling. The shared `doAuthedRequest` helper in
`targets.go` builds the request manually (rather than using the
generated `ClientWithResponses`) because the target verbs need
fine-grained 4xx classification: 404 carries the resolver's
structured `{"error": "no_target", "query": "...", "matches": [...]}`
envelope, 409 carries `ambiguous_target` with colliding names, and
501 carries the "no connector registered" detail. The hand-written
`renderHTTPError` ladder classifies each status into the right
`output.StructuredError` category.

### Exit codes

- `0` — verb ran cleanly. `list` exits 0 on an empty tenant
  (operationally meaningful, never 404).
- `2` — `auth_expired` (no stored credentials, refresh exhausted, or
  401 after the one-shot retry).
- `3` — `unreachable` (network / transport failure before the
  backplane responded).
- `4` — `unexpected_response` (404 not-found, 409 ambiguous, 501
  no-connector, 500 connector exception, malformed JSON, etc.).
- `5` — `insufficient_role` (403 RBAC denial; backend's detail string
  names the required role).

### Out of scope (v0.2)

- Write verbs (`create` / `update` / `delete`). The API supports them
  (require `tenant_admin`); the CLI surfaces them in a follow-up
  task when operators ask. Bulk import via T6 (#257) lands in a
  sibling PR.
- Auto-completion of target names. Operators type names; tab-completion
  would need a separate `cobra-complete`-style design pass.
- Client-side caching. Every CLI invocation hits the API fresh — the
  source of truth is the backplane, not a stale local copy.

## Vault alias verbs (`meho vault`, G3.3-T6 #550)

`cli/internal/cmd/vault/` registers the operator-facing alias verb
tree for the `vault-1.x` typed connector (Initiative #366). It is the
same pattern as the `vmware` tree (G3.1-T7 #511): a thin cobra layer
that pre-bakes one `connector_id` so operators don't type it on every
dispatch. Every verb POSTs to `POST /api/v1/operations/call` — the
same G0.6 dispatcher route the agent surface uses — so auth, policy,
audit, JSONFlux, and broadcast all run identically whether an agent
calls `call_operation` or an operator runs `meho vault …`. Per
[CLAUDE.md](../../CLAUDE.md) postulate 5 these alias verbs are
operator-only ergonomics and are **not** mirrored on the MCP surface.

`ConnectorID = "vault-1.x"` is the dispatcher's natural-key encoding
of `(product="vault", version="1.x", impl_id="vault")`, pinned by the
backend connector-id-parse contract test. A future re-versioning is a
single-line edit.

### Subcommands

- `meho vault kv read|list|put|versions|delete <mount> <path>` —
  the KV-v2 group (`vault.kv.*`, ops registered by G3.3-T1 #545). The
  `<mount> <path>` positional pair maps to `params.mount` /
  `params.path`; the CLI always sends `mount` explicitly so the
  operator's choice is authoritative (no client-side default that
  could drift from the handler's `"secret"`). `put` takes
  `--data '<json>'|@<file>` and an optional `--cas N`
  (check-and-set; only sent when explicitly passed). `delete` takes
  `--versions 3,4,5` (parsed client-side to `[]int` so a bad value is
  an argv error, not a backend schema-rejection round-trip). `read`
  replaces the consumer's `_secret-read.sh secret/<mount>/<path>`
  wrapper.
- `meho vault sys health|seal-status|mounts-list|auth-list` — read-
  only diagnostics (`vault.sys.*`, G3.3-T2 #546). No args, no params.
- `meho vault auth userpass-list|userpass-read <user>|approle-list|approle-read <role>`
  — read-only identity browse (`vault.auth.*`, G3.3-T3 #547). The
  `read` verbs map their single positional to the op's schema key
  (`username` for userpass, `role_name` for approle).

### Reserved flags (same shape across every verb)

- `--target <slug>` — the Vault target the dispatcher resolves
  server-side (sent as `{"name": "<slug>"}`; absent → `null` on the
  wire).
- `--json` — emit the raw `OperationResult` envelope instead of the
  human render.
- `--backplane <url>` — override the backplane URL (defaults to the
  URL recorded by `meho login`).

### Output discipline

Vault payloads (secret data, metadata, version maps, mount maps) are
nested JSON the operator reads as a tree, so every verb uses the
generic indented-JSON renderer rather than a per-shape table — a
per-op table buys little over the dump while risking contract-drift
panics. Set-shaped responses (`vault kv list`,
`vault auth userpass-list`, …) arrive **already reduced** to the
JSONFlux sample + result-handle envelope by the dispatcher; the CLI
prints that verbatim with the handle hint intact, consistent with the
`vmware` sibling. Operators drill into a handle with the
`meho operation` result verbs.

### HTTP shape + exit codes

Identical to the `meho operation` surface (the verbs are pre-scoped
wrappers over the same route): bearer injection + one-shot
401-refresh-retry via `api.NewAuthedClient`, hand-written
`CallResult` / `callRequestBody` structs mirroring the backend
Pydantic models (duplicated per package to avoid the cmd/* import
cycle cmd/root.go's graft would otherwise create). Exit codes: `0`
status=ok, `1` status=error/denied (via the `errOpError` sentinel),
`2` auth_expired, `3` unreachable, `4` unexpected_response.

## Topology verbs (`meho topology`, G9.1-T6 #454 + G9.2-T6 #599)

`cli/internal/cmd/topology/` registers seven operator-facing topology
verbs that wrap the T5 REST surface (#453 / #597). The eighth
G9.1-T6 verb, `meho targets discover`, lives under the `meho targets`
parent (`cli/internal/cmd/targets/discover.go`) because the backend
registers `GET /api/v1/targets/discover` on the targets router, under
the canonical `/api/v1/targets` prefix.

### Subcommands (G9.1 read/traversal — #454)

- `meho topology refresh <target>` — `POST
  /api/v1/topology/refresh/<target>`. Rediscovers one target's
  topology and reconciles it into the graph; renders the per-target
  `nodes: +A -R ~U` / `edges: +A -R ~U` count summary. The backend
  resolves `<target>` tenant-scoped, so a cross-tenant target 404s
  identically to a typo (cross-tenant refresh is impossible by
  construction).
- `meho topology dependents <name|alias> [--depth N] [--kind K]
  [--node-kind K]` — `GET /api/v1/topology/dependents/<name>`.
  Reverse closure ("what depends on me" — the blast-radius verb
  consumer-needs.md L258 specifies, run *before* a destructive op).
  Renders a depth-ordered `DEPTH / KIND / NAME / VIA` table; the
  anchor is row 0 (empty VIA) so an operator distinguishes "exists,
  no dependents" (one row) from "not in this tenant" (zero rows).
- `meho topology dependencies <name|alias> [--depth N] [--kind K]
  [--node-kind K]` — `GET /api/v1/topology/dependencies/<name>`.
  Forward closure ("what I depend on") — the mirror of `dependents`,
  same table shape and contract, opposite walk direction.
- `meho topology path <from> <to> [--max-hops N] [--from-kind K]
  [--to-kind K]` — `GET /api/v1/topology/path?from=A&to=B`. Shortest
  unweighted path rendered as a `kind/name -> … (N hops)` chain, or
  the no-path line when unreachable / an endpoint is missing /
  cross-tenant (all the same `null` answer, exit 0, never an error).

### Subcommands (G9.2 curated-edge write + listing — #599)

- `meho topology annotate <from> <kind> <to> [--note "..."]
  [--evidence-url URL] [--from-kind K] [--to-kind K]` — `POST
  /api/v1/topology/edges`. Asserts a curated cross-system edge.
  Idempotent (server-side upsert). `--help` inlines the closed
  10-kind vocabulary table (§12 of Initiative #364) so operators
  discover valid `<kind>` values without leaving the CLI.
  `--evidence-url` is kebab-case on the CLI but maps to the wire
  field `evidence_url` (snake_case per `_AnnotateEdgeRequest`).
  Requires `tenant_admin`; a 403 renders the backend's role hint
  with exit class `insufficient_role`.
- `meho topology unannotate <edge-id> | <from> <kind> <to>
  [--from-kind K] [--to-kind K]` — `DELETE
  /api/v1/topology/edges/<edge_id>`. The tuple form is **client-
  side**: a `GET /api/v1/topology/edges?from=&kind=&to=&source=
  curated` resolves the unique curated edge, then `DELETE` removes
  it by id. T5's DELETE is id-only (no tuple-form route), so the
  resolution must happen here. The route's typed 409 (auto-row
  deletion refused; §3 of Initiative #364) is rendered with the
  server's `detail.message` verbatim — the annotate-over-auto
  remediation guidance, not a raw HTTP dump. Requires `tenant_admin`.
- `meho topology list-edges [--kind K] [--source curated|auto]
  [--from N] [--to N] [--conflicts] [--limit N] [--offset N]` —
  `GET /api/v1/topology/edges`. Flat filterable listing of the
  tenant's edges. `--source` maps directly to the `graph_edge.source`
  column literal; `--conflicts` surfaces the §6 conflict-detector
  recoverability listing only. Default output is an aligned
  `KIND / SOURCE / FROM / TO / LAST_SEEN` table; `--json` emits the
  raw `[]Edge` envelope so consumers can pipe ids into the
  `unannotate <edge-id>` form. Role: `operator`.

### Flag → query-param mapping

The route exposes `kind` (anchor `(tenant_id, kind, name)` pin) and
`kind_filter` (walk-edge filter) as two distinct params. Per the #454
spec `--kind <edge_kind>` is the **edge** filter, so `--kind` maps to
`kind_filter`; the separate `--node-kind` flag maps to `kind` and is
the remedy the 409 `ambiguous_node` render points at ("re-run with
--node-kind …"). `path` maps `--from-kind`/`--to-kind` →
`from_kind`/`to_kind` and `--max-hops` → `max_hops`. `--depth`
(1..64) and `--max-hops` (1..32) mirror the API's `Query(le=…)`
ceilings and fail fast client-side (no 422 round-trip), the same
discipline `meho targets list --limit` applies.

### Reserved flags (every verb)

- `--json` — emit the raw envelope to stdout instead of the human
  render. Stable schemas: `refresh` → `RefreshResult`;
  `dependents`/`dependencies` → `[]Node`; `path` →
  `Path` or literal `null` (the unreachable answer, emitted
  verbatim so jq consumers see one contract); `annotate` →
  `TopologyEdge` (the 201 response shape); `unannotate` →
  `{"deleted": "<edge_id>"}` on success; `list-edges` →
  `[]TopologyEdge`.
- `--backplane <url>` — override the backplane URL (defaults to the
  URL `meho login` recorded).

### HTTP shape + exit codes

Same in-package `resolveBackplane` / `doAuthedRequest` /
`renderRequestError` trio every sibling verb tree carries (the
shared-helper-vs-import-cycle reason `kb.go` documents — Initiative
#363 names a `cli/internal/api_client/topology.go`, but the codebase
convention supersedes that path; the intent is satisfied in-package).
`renderHTTPError` adds the topology-specific 409 `ambiguous_node`
classifier (names the colliding kinds + the `--node-kind` remedy) and
reuses the resolver's structured 404 near-miss formatter for
`refresh`. Exit codes: `0` ok (including empty closure / no drift /
no path — all operationally meaningful, never 404), `2` auth_expired,
`3` unreachable, `4` unexpected_response (404 / 409 / malformed),
`5` insufficient_role (403; backend names the required role).

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

```text
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
  `v0.27.0`; the Go 1.22 minimum constraint that previously blocked
  upgrades is lifted now that the module requires Go 1.25.8.
* `github.com/oapi-codegen/runtime` — runtime helpers the generated
  client uses (JSON merging for `oneOf` unions, parameter styling
  per RFC 6570). Pinned at `v1.1.1`; the Go 1.22 compatibility
  constraint that blocked upgrades is lifted now that the module
  requires Go 1.25.8 — upgrade tracked as a follow-up.

Build-time tool (not in `go.mod`; installed under `bin/` via
`make tools`):

* `github.com/oapi-codegen/oapi-codegen/v2` — the OpenAPI → Go
  client generator itself. Pinned at `v2.5.0`, the last v2.x
  release whose module go directive was compatible with Go 1.22;
  now that the module requires Go 1.25.8, a newer v2.x may be
  used — upgrade tracked as a follow-up. `make tools` runs
  `go install …@v2.5.0` with
  `GOBIN=$PWD/bin`; the generator itself executes on a Go 1.24+
  toolchain that Go downloads automatically (the `go install`
  command honours the dep's `go` directive).

Indirect transitive deps tracked via `go mod tidy` in `go.sum`. The
project keeps the dep graph small — every transitive import is one
more thing supply-chain scanning has to vouch for, and operators
have to trust to run `meho login` against their secrets.

## Admin Keycloak bootstrap (`meho admin keycloak bootstrap-clients`, G0.9.1-T11 #791)

`cli/internal/cmd/admin/keycloak/` registers the install-time
realm-provisioning verb that closes the v0.3.1 dogfood's deepest
deployer friction: a working `meho-cli` device-code client + the
public MCP browser-flow client + the **5 protocol mappers + 4 default
client scopes + meho-admins group + admin user** that together let
`meho login` and the MCP onramp authenticate without 4 sequential
walls of opaque `invalid_token` / `unauthorized_client` errors. The
verb encodes the 5-step recipe documented in
[`deploy/values-examples/README.md`](../../deploy/values-examples/README.md)
§ Auth onramp recipe — that doc remains the manual path for realms
where the admin API isn't reachable; this verb is the automation when
it is.

Unlike the rest of the CLI tree (which dispatches through the
backplane via the G0.6 dispatcher route), `meho admin keycloak`
talks **directly** to a Keycloak admin REST API using operator
credentials. The verb is a one-shot install-time helper, not an
agent-facing operation, and is **not** mirrored on the MCP surface.

### Subcommands

- `meho admin keycloak bootstrap-clients` — idempotently reconcile a
  realm against the recipe:
  1. Public device-code client (default name `meho-cli`,
     `publicClient=true`, `oauth2DeviceAuthorizationGrantEnabled=true`,
     every other flow off).
  2. Public authorization-code+PKCE MCP client (default name
     `meho-mcp-client`, `standardFlowEnabled=true`,
     `pkce.code.challenge.method=S256`, redirect URIs for Claude.ai +
     localhost MCP Inspector).
  3. 5 protocol mappers cloned from the reference shape on
     `meho-backplane`, installed on **both** public clients:
     `audience-meho-backplane`, `meho-mcp-audience`, `tenant-id`,
     `tenant-role`, `groups-claim`.
  4. 4 default client scopes (`basic`, `roles`, `web-origins`,
     `acr`) explicitly assigned to **both** public clients. The
     `basic` scope is load-bearing — Keycloak 25+ moved the `sub`
     claim mapper into it, and clients created via the admin API do
     **not** auto-inherit the realm's default-default scopes, so an
     explicit assignment is the only way to guarantee `sub` lands in
     the access token (RFC 9068 §2.2.1 requires it).
  5. The `meho-admins` top-level group + an admin user joined to
     it, with a password set via `/users/{id}/reset-password`.
  6. Optional client scope `offline_access` on the MCP client only —
     the realm's built-in `offline_access` scope is attached to
     `meho-mcp-client` as **optional** (not default — only flows that
     ask for a refresh token mint one). The CLI device-code client
     (`meho-cli`) deliberately does **not** get it: RFC 8628
     device-code clients re-run the device dance rather than hold a
     long-lived refresh token, and a stolen device-code refresh token
     has worse blast-radius than re-prompting the operator. Closes
     the W7 wall of `deploy/values-examples/README.md` (#912).

### Idempotency

Every step does a "does this exist?" check before mutating:

- Clients: `GET /clients?clientId=<id>`; on hit, PUT to update; on
  miss, POST to create.
- Mappers: `GET /clients/{uuid}/protocol-mappers/models`; missing
  mapper → POST; existing-but-different → PUT; existing-and-equal →
  skip.
- Default scopes: `GET /clients/{uuid}/default-client-scopes`;
  missing scope → PUT; already present → skip.
- Optional scopes: `GET /clients/{uuid}/optional-client-scopes`;
  missing scope → PUT; already present → skip. Only applied to the
  MCP client (the CLI client's optional-scope set is left untouched
  for the RFC 8628 rationale above).
- Group: `GET /groups?search=<name>` (filtered client-side to exact
  match); missing → POST.
- User: `GET /users?exact=true&username=<name>`; missing → POST then
  `PUT /users/{id}/reset-password`; existing → skip the password
  reset (silent password rotation on a re-run is strictly worse than
  a "set it once at create time" rule — see the per-finding rationale
  in `reconcileUser`).

A clean re-run prints `[skip]` for every resource and exits 0.

### Refusals

The verb refuses operator-friendly mistakes at the validation
boundary:

- `--cli-client-id meho-backplane` (or `--mcp-client-id
  meho-backplane`) → refuses with a one-line explanation that
  `meho-backplane` is the confidential resource-server client and is
  out of scope.
- `--mcp-resource-uri` with a trailing slash → refuses, because the
  backplane normalises `MCP_RESOURCE_URI` server-side and the
  audience claim in the token must match the no-slash form.
- `--skip-user-provisioning` omitted but `--admin-user-username` /
  password unset → refuses with the specific missing-flag name.

### Secret handling

Two passwords flow through the verb: the **master-realm admin
password** (used to mint the admin token via the password grant
against the built-in `admin-cli` client) and the **new admin user's
password**. Both are read from env vars (`KEYCLOAK_ADMIN_PASSWORD` /
`KEYCLOAK_ADMIN_USER_PASSWORD`) or stdin; neither is ever accepted
via a command-line flag, so neither lands in shell history, `ps`
output, or process supervisor logs. The pattern mirrors the
reference shell script's mode-600 tempfile dance, adapted for Go's
stdin reader.

### HTTP client

Stdlib `net/http` + `encoding/json` — no Keycloak Go SDK in
`go.mod`. The admin verb's surface area is small (clients +
protocol-mappers + client-scopes + users + groups, all under
`/admin/realms/{realm}/...`); pulling in a generated SDK for that is
a bad supply-chain tradeoff. The same discipline as the rest of the
CLI: every transitive import has to justify its place in `go.sum`.

The `--insecure-skip-tls-verify` flag flips `tls.Config.InsecureSkipVerify`
on a custom transport for the one-time bootstrap case where the
operator workstation has not yet trusted the realm's internal CA.
The flag is opt-in and explicit; the default uses the system trust
store via `http.DefaultTransport`.

### Tests

`bootstrap_test.go` drives a fake Keycloak (`httptest.Server` +
in-memory state maps) through eight scenarios:

- Fresh realm: every resource created, mapper + scope counts match
  the recipe (5 mappers, 4 default scopes, 2 clients, 1 group, 1
  user).
- Idempotent re-run: zero new POSTs against the same realm; password
  reset called exactly once across two runs.
- Confidential-client refusal: `--cli-client-id meho-backplane`
  errors at the validation boundary.
- Trailing-slash refusal: `--mcp-resource-uri .../mcp/` errors with
  a "trailing slash" message naming the recipe rule.
- Dry-run: zero Keycloak calls, banner present in stdout.
- Skip-user-provisioning: 2 clients land, 0 groups, 0 users.
- Mandatory-flag validation: missing `--keycloak-base-url`,
  `--realm`, etc. each surface a flag-specific error.
- Mapper-shape parity with the reference shell script:
  `audience-meho-backplane` carries `included.client.audience=
  meho-backplane`; `meho-mcp-audience` carries `included.custom.
  audience=<uri>`; `tenant-id` / `tenant-role` are
  `oidc-hardcoded-claim-mapper`; `groups-claim` is
  `oidc-group-membership-mapper` with `claim.name=groups`.

Real-realm verification belongs in a future `testcontainers` Keycloak
integration test; the unit suite proves the orchestrator's
interaction shape, not the realm semantics.

## Known issues / forward-compat scaffolding

* `meho version` prints CLI metadata only. The Goal #11 contract
  also calls for a backplane-version line; this is now feasible
  (the AuthedClient can call `GET /version`) but deferred until
  Initiative G2.7 wires its CI seam so the format choice doesn't
  thrash. Filed as a follow-up adjacent to T3.
* Persistent `--config` and `-v/--verbose` flags are registered on
  the root command but not yet consumed; reserved for v0.2.
* The auth-config endpoint at `/api/v1/auth-config` shipped in v0.3.1
  (issuer + audience) and was completed in v0.3.2 (G0.9.1-T9) with
  the `cli_client_id` field that drives the CLI's device-code
  `client_id`. Operators on a backplane older than v0.3.2 (or one
  where `KEYCLOAK_CLI_CLIENT_ID` was never wired) get an actionable
  public-client error from `meho login` and the `--issuer`/
  `--client-id` overrides as the documented escape hatch.
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

## Targets registry (`meho targets`, G0.3 #224)

`cli/internal/cmd/targets/` registers cobra verbs for the G0.3
targets registry (Initiative #224). The v0.2 surface ships:

- `meho targets import <file>` (G0.3-T6 #257) — bulk-import a
  `targets.yaml` file. Sibling verbs (`list`, `describe`, `probe`)
  land separately via G0.3-T5 #256.

### Import verb

`import.go` implements `meho targets import <file>` with the
flags called out in the issue body: `--update` (PATCH existing
targets instead of erroring), `--dry-run` (print the plan; no API
calls), `--json` (structured plan output), `--backplane` (override
the configured backplane URL).

**Mapping rules.** The CLI parses the YAML as a generic
`map[string]any` per entry and partitions every key:

- **Known top-level columns** map 1:1 to the API's `TargetCreate` /
  `TargetUpdate` body fields: `name`, `aliases`, `product`, `host`,
  `port`, `fqdn`, `secret_ref`, `auth_model`, `vpn_required`,
  `notes`, `preferred_impl_id`. The list in
  `knownTopLevel` is the canonical reference; the
  Python-side mirror lives in
  `backend/tests/test_api_v1_targets_import.py:_KNOWN_TOP_LEVEL` and
  keeps drift detectable in CI.
- **`fingerprint`** is dropped silently with a warning log line.
  Server-managed per the G0.3-T1.5 (#477) amendment — the probe
  verb is the only legitimate writer, and the API rejects
  caller-supplied values with 422 via
  `model_config = ConfigDict(extra='forbid')`. Skipping at the CLI
  is friendlier than letting the import abort on a 422 the operator
  can't fix without editing the source YAML.
- **`preferred_impl_id`** is a real top-level column post-#477.
  Sent at the body root, not spilled into extras — the G0.6 #388
  resolver's tie-break ladder reads it.
- **Every other key** spills into the `extras` JSONB column.
  Explicit `extras:` blocks in the YAML merge with spilled keys
  rather than overwriting them.

**Idempotency.** The plan-build phase fetches
`GET /api/v1/targets` (paginated) and partitions every YAML entry
into `CREATE` (no existing match) vs `UPDATE` (name already exists
in tenant). Default mode aborts the whole import on the first
duplicate — operators have to re-run with `--update` to opt into
PATCH semantics. The plan is built before *any* write fires, so a
partial-conflict YAML never leaves the tenant half-imported.

**Sparse-PATCH contract.** The PATCH body for each updated entry
is sparse: only keys present in the YAML appear, with `name` and
`product` stripped (immutable post-create). This is load-bearing —
without it the route handler's
`updates = body.model_dump(exclude_unset=True); for k, v in updates: setattr(t, k, v)`
loop combined with Pydantic v2's "explicit null counts as set"
semantics is PUT-shaped, not PATCH-shaped, and would wipe every
column the YAML omits on every `--update` run. PR #362's review on
issue #257 (2026-05-14) surfaced this bug in an earlier draft; the
`entryToUpdateBody` helper is the fix.

### HTTP shape

The verb routes through `api.NewAuthedClient` for bearer injection
+ 401-refresh-retry, same as `meho status` and `meho operation
call`. The shared `doAuthedRequest` helper inside `import.go` is
adapted to an `httpDoer` function-shape so unit tests can drive
the plan / execute path against an in-process `fakeDoer` without
the auth/token-store machinery (which is independently covered by
`cli/internal/auth`'s own tests).

The helper is duplicated from `cmd/operation/operation.go` because
`cmd/operation` can't be imported from `cmd/targets` without an
import cycle (both packages are grafted onto the same tree by
`cmd/root.go`). If a third subcommand package grows, the duplicated
helper should be extracted to a shared `cmd/_authed` package.

### Tests

- `import_test.go` — Go unit tests for the YAML parser, the
  mapping rules (top-level / extras spill / fingerprint skip /
  preferred_impl_id top-level), the sparse-PATCH body shape, the
  plan partitioning logic, and the dry-run code path.
- `backend/tests/test_api_v1_targets_import.py` — Python
  integration tests against `/api/v1/targets` exercising the
  CREATE / PATCH semantics the CLI relies on. The
  real-`targets.yaml` round-trip test replays every conformant
  entry from a pinned snapshot of
  [`evoila-bosnia/claude-rdc-hetzner-dc/rdc-hetzner-dc/targets.yaml`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/rdc-hetzner-dc/targets.yaml)
  (24 entries; SHA pinned in the test module).

### Out-of-scope

- Export (`meho targets export > file.yaml`) — v0.2.next polish.
- Bulk delete via YAML — explicit out-of-scope on the issue.
- Cross-tenant migration — operators import into their JWT's tenant.
- Watching `targets.yaml` for changes — out-of-scope.
- Schema validation against a Pydantic-equivalent on the CLI side —
  CLI does minimal local validation (`name`, `product`, `host` are
  required); the API does the strict validation and errors
  propagate.

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
