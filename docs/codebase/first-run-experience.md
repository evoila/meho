<!--
SPDX-License-Identifier: AGPL-3.0-only
Copyright (c) 2026 evoila Group
-->

# First-run experience

## Overview

MEHO's first-run experience is the path a cold evaluator takes from a fresh
`git clone` to a running UI. This document maps that path — the files involved,
the decision points, the intended behavior, and the places where today's
documentation drifts from today's code. It is the shared reference for the
initiatives under Goal #254.

The evaluator persona is explicit: a developer who found MEHO through external
channels, has no tribal knowledge of the repository, and has roughly ten minutes
of patience before deciding whether to continue.

## Key files

- [README.md](../../README.md) — the literal first surface. "Quick Start"
  section is the golden path.
- [docker-compose.yml](../../docker-compose.yml) — the community-edition
  compose stack. Drives profile behavior, service ordering, and default
  environment.
- [env.example](../../env.example) — the template every evaluator copies to
  `.env`. Defaults here drive first-run outcomes even when the user barely
  touches the file.
- [docs/troubleshooting.md](../troubleshooting.md) — today scoped to connector
  issues; will be extended with a first-run section.
- [scripts/dev-env.sh](../../scripts/dev-env.sh) — the team's development
  entrypoint. Not evaluator-facing, but its `needs_tei_profile` auto-activation
  logic is the reference implementation for the profile-selection behavior the
  README must describe.
- [meho_app/modules/knowledge/embeddings.py](../../meho_app/modules/knowledge/embeddings.py) —
  the runtime embedding-provider selection lives in `get_embedding_provider()`.

## Design principle: Voyage-primary, local TEI fallback

MEHO is built around Voyage AI as the primary embeddings provider. Voyage gives
the fastest first run, works identically on every host architecture, and is the
path an operator with a production budget is expected to use. The local TEI
fallback exists for evaluators who are unwilling or unable to create a Voyage
account — it keeps MEHO self-contained and usable offline, at the cost of
slower first boot and limited architecture support.

This framing is asymmetric on purpose. Plan A is the path the product is built
for; the fallback is a labeled alternative, not a default. Documentation should
reflect the asymmetry explicitly: evaluators who follow the happy path should
end up on Voyage, and the fallback should be surfaced as a deliberate choice
with understood trade-offs.

## The evaluator funnel

The first-run experience is a funnel with three decision points. Each point
represents a place where an evaluator can drop off if the path is unclear or
the system produces an unexpected state:

1. **Quick Start commands succeed** — the documented command produces a
   running stack with no manual recovery.
2. **UI loads at `http://localhost:5173`** — the frontend is reachable and
   does not redirect into an error state.
3. **First real interaction succeeds** — a chat message produces a non-error
   response.

Goal #254 is about plugging leaks at the first two stages. Everything after is
covered by product quality, not onboarding.

## The two-axis decision matrix

Two axes determine the correct command for an evaluator:

- **Hardware**: x86_64 (Intel Mac, Linux, Windows WSL) vs arm64 (Apple
  Silicon, ARM Linux).
- **Provider**: Voyage (plan A, requires API key) vs fully local (uses the
  TEI fallback, no external account).

These combine into four cells:

| | Voyage | Fully local |
|---|---|---|
| **x86_64** | Fastest path. `docker compose up` with both API keys set. | Works natively. `docker compose --profile tei up`. First boot downloads ~2 GB of model weights. |
| **arm64** | Identical to x86_64. Voyage is architecture-agnostic. | Works under Rosetta 2 emulation. TEI image is x86_64-only today; Rosetta translates it transparently but model loading is slower and memory overhead is higher. Native arm64 TEI is deferred to a post-launch follow-up. |

The README should expose this matrix explicitly so evaluators can self-select.

## What `docker compose up` does today

The default compose profile starts every service that does **not** declare a
`profiles:` key. This means:

- **Started by default**: `postgres`, `minio`, `redis`, `keycloak`, `seq`,
  `meho`, `meho-frontend`.
- **Skipped by default**: `tei-embeddings` (`profiles: ["tei"]`),
  `tei-reranker` (`profiles: ["tei"]`), `pgadmin` (`profiles: ["tools"]`),
  `ollama` (`profiles: ["ollama"]`).

An evaluator who runs the literal `docker compose up` command with no Voyage
key gets a stack where the embeddings backend is not running. The backend
auto-selects TEI based on the absence of `VOYAGE_API_KEY`, tries to reach
`http://tei-embeddings:80`, and fails on the first knowledge query with a
connection-refused error surfaced as a generic backend 500.

The path that works today without tribal knowledge is either of:

- `docker compose up` with `VOYAGE_API_KEY` set in `.env`.
- `docker compose --profile tei up` with only `ANTHROPIC_API_KEY` set.

The README should present these as two explicitly labeled paths rather than
implying a single "just run compose up" command.

## Provider selection at runtime

`get_embedding_provider()` in
[meho_app/modules/knowledge/embeddings.py](../../meho_app/modules/knowledge/embeddings.py)
selects a provider once per process and caches the result:

- If `VOYAGE_API_KEY` is set in config → `VoyageAIEmbeddings` is instantiated
  with the key and the `EMBEDDING_MODEL` name (`voyage-4-large` by default).
- Otherwise → `TEIEmbeddings` is instantiated against `TEI_EMBEDDING_URL`
  (which defaults to `http://tei-embeddings:80` inside the compose network).

This logic is correct and stable. The failure mode in the "default profile"
case is not that selection is broken — it is that the TEI service itself is
never started, so the selected `TEIEmbeddings` client has nothing to talk to.

## Architecture support

Every base image in the compose stack ships native arm64 **except** the TEI
embedding/reranker image:

| Service | Image | Native arm64? |
|---|---|---|
| postgres | `pgvector/pgvector:pg15` | yes |
| minio | `minio/minio:latest` | yes |
| redis | `redis/redis-stack-server:latest` | yes |
| keycloak | `quay.io/keycloak/keycloak:24.0` | yes |
| seq | `datalust/seq:latest` | yes |
| pgadmin | `dpage/pgadmin4:latest` | yes |
| ollama | `ollama/ollama:latest` | yes |
| **tei-embeddings / tei-reranker** | `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9` | **no — x86_64 only** |

The arm64 story is entirely about one image. The rest of the stack runs
natively on Apple Silicon without special configuration. TEI is pulled as an
amd64 image and executed under Docker Desktop's Rosetta 2 emulation on arm64
Macs. Rosetta is **disabled by default** in recent Docker Desktop releases —
evaluators must enable it in Settings > General > "Use Rosetta for x86_64/amd64
emulation on Apple Silicon" before the TEI profile will run. The
`docker-compose.yml` TEI service blocks declare `platform: linux/amd64`
explicitly so Docker pulls the correct manifest on arm64 hosts (without this
directive, `docker compose --profile tei up` fails immediately with
`no matching manifest for linux/arm64/v8`). Measured baselines — first-boot
time, steady-state latency, peak memory — live in
[docs/development/arm64-notes.md](../development/arm64-notes.md); the
troubleshooting doc covers the Rosetta enablement flow.

`meho` and `meho-frontend` are built locally by Compose from the unified
`docker/Dockerfile.meho` (backend) and `docker/Dockerfile.meho-frontend`
(frontend). They inherit the host architecture from BuildKit automatically,
so they are native arm64 on an arm64 host. The release workflow publishes
prebuilt multi-arch images to `ghcr.io/evoila/meho-backend` for
`linux/amd64` and `linux/arm64`, so evaluators who pull the published image
on Apple Silicon get a native arm64 binary — no Rosetta fallback for the
backend itself.

## Runtime-config propagation

Four values change per deploy and must reach the browser: `API_URL`,
`KEYCLOAK_URL`, `KEYCLOAK_REALM`, and `KEYCLOAK_CLIENT_ID`. They are written
into `window.__RUNTIME_CONFIG__` by a tiny script, `/config.js`, that the
frontend bundle consumes via [meho_frontend/src/lib/config.ts](../../meho_frontend/src/lib/config.ts).

The pipeline has two boundaries, not one:

1. **Build time (Vite).** `VITE_*` environment variables are inlined into
   hashed bundle assets under `assets/index-*.js`. Cache-safe because the
   filename carries a content hash — any change to the underlying value
   produces a new filename, so `Cache-Control: public, immutable` with a
   one-year expiry is correct.
2. **Container startup (envsubst).** The `CMD` in
   [docker/Dockerfile.meho-frontend](../../docker/Dockerfile.meho-frontend)
   rewrites `config.js.template` into `/usr/share/nginx/html/config.js`
   using the live environment variables. The filename is stable across
   deploys, so the browser must be told to refetch on every page load.

The second boundary is why `/config.js` gets its own
[nginx location block](../../nginx.conf) declaring
`Cache-Control: no-store`. Two quirks of nginx make that block larger than
it looks:

- **Exact match short-circuits regex.** `location = /config.js` wins against
  the broader `location ~* \.(js|css|...)$` wildcard regardless of file
  order — the exact match terminates the search immediately. The rule is
  placed first in the file anyway so the reading order matches the
  evaluation order.
- **`add_header` is not inherited once a child overrides it.** The moment a
  child `location` declares any `add_header`, nginx drops every parent
  `add_header` for the response, including those marked `always`. Every
  security header (XFO, nosniff, XSS, Referrer-Policy, HSTS,
  Permissions-Policy, CSP-Report-Only) has to be repeated inside the
  `/config.js` block to match the hardened server defaults.

The same structure lives in both [nginx.conf](../../nginx.conf) (fallback
for local/non-Docker use) and [nginx.conf.template](../../nginx.conf.template)
(envsubst template consumed by the container). The template substitutes
`${ALLOWED_ORIGINS}` and `${KEYCLOAK_ORIGIN}` into the CSP at startup; the
rest of the block is identical to the fallback.

A regression test at
[tests/unit/test_nginx_config.py](../../tests/unit/test_nginx_config.py)
asserts the structural contract — the `/config.js` block exists in both
files, declares `no-store`, does not declare `immutable`, and repeats every
required security header; the wildcard block still serves hashed assets as
`public, immutable`.

If `/config.js` traffic ever becomes non-trivial (today it is roughly 150
bytes per page load) the cleaner fix is to emit the filename with a
content hash at build time and drop the `no-store` contract entirely.
That refactor is deferred — the build pipeline would need to thread the
hash into `index.html`, and the envsubst step would need to produce a
correspondingly-named artifact. `no-store` is the minimum-viable fix and
the chosen shape for launch.

## Known issues

- **README golden path is broken by default.** Running the literal Quick Start
  on a fresh clone with no Voyage key produces a stack where the embeddings
  backend is not reachable. Tracked under Goal #254.
- **README, env.example, and compose disagree on the default provider.** The
  README claims "Local TEI by default," env.example ships
  `EMBEDDING_MODEL=voyage-4-large` unconditionally, and the runtime code
  auto-selects based on key presence. Three sources, three different answers.
- **Stale `claude-haiku-4-6` model IDs in env.example.** Five utility-tier
  env vars reference a non-existent model. Any first-run path through the
  classifier or data-extractor returns `model_not_found` from Anthropic.
- **README sets no architecture expectations.** Apple Silicon is the single
  largest evaluator population and is never mentioned.
- **No first-run troubleshooting content.** `docs/troubleshooting.md` is
  entirely connector-facing.

## References

- [README.md](../../README.md) — Quick Start section
- [docker-compose.yml](../../docker-compose.yml) — compose stack and profiles
- [env.example](../../env.example) — default environment template
- [meho_app/modules/knowledge/embeddings.py](../../meho_app/modules/knowledge/embeddings.py) —
  runtime provider selection
- [scripts/dev-env.sh](../../scripts/dev-env.sh) — team dev entrypoint with
  auto-profile logic, reference implementation
- [scripts/preflight.sh](../../scripts/preflight.sh) — evaluator-side host
  and `.env` validation; runs before `docker compose up`
- [scripts/validate-install.sh](../../scripts/validate-install.sh) — maintainer
  smoke command; runs preflight + post-up probes (backend, frontend, Keycloak,
  real chat roundtrip). Used by the release owner before cutting a release;
  intentionally not wired into CI (Goal #294 owns the CI bootstrap smoke job)
- [docs/development/arm64-notes.md](../development/arm64-notes.md) — measured
  Rosetta baseline and arm64 operational notes
- [docs/codebase/bootstrap-and-migrations.md](bootstrap-and-migrations.md) —
  companion document for the bootstrap layer owned by Goal #294
- [docs/codebase/public-mirror.md](public-mirror.md) — companion document for
  the mirror pipeline that ships this README publicly
- Goal #294 — bootstrap and migrations refactor (owns everything inside
  `docker/Dockerfile.meho`, the Alembic tree, and the migration entrypoint)
- [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) — format
  expectation for release-hygiene content
