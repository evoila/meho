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
Macs. Rosetta is enabled by default on recent Docker Desktop releases but can
be disabled in settings; the troubleshooting doc must cover how to verify and
enable it.

`meho` and `meho-frontend` are built locally by Compose from Dockerfiles in
`docker/`. They inherit the host architecture from BuildKit automatically, so
they are native arm64 on an arm64 host. If a future change introduces
prebuilt images published to `ghcr.io/evoila/meho-backend`, that publication
step must produce a multi-arch manifest using
`docker buildx build --platform linux/amd64,linux/arm64`, or arm64 evaluators
will silently run the backend under Rosetta as well. That requirement belongs
to the unified-Dockerfile work in Goal #294 and is tracked via cross-reference.

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
- **No preflight script.** Evaluators cannot detect obvious problems (port
  conflicts, missing env vars, Docker version, disk space) before starting
  the stack.
- **TEI arm64 fallback is implicit Rosetta, undocumented.** The fallback
  works but the performance trade-off is not measured or published anywhere
  in the repository.

## References

- [README.md](../../README.md) — Quick Start section
- [docker-compose.yml](../../docker-compose.yml) — compose stack and profiles
- [env.example](../../env.example) — default environment template
- [meho_app/modules/knowledge/embeddings.py](../../meho_app/modules/knowledge/embeddings.py) —
  runtime provider selection
- [scripts/dev-env.sh](../../scripts/dev-env.sh) — team dev entrypoint with
  auto-profile logic, reference implementation
- [docs/codebase/bootstrap-and-migrations.md](bootstrap-and-migrations.md) —
  companion document for the bootstrap layer owned by Goal #294
- [docs/codebase/public-mirror.md](public-mirror.md) — companion document for
  the mirror pipeline that ships this README publicly
- Goal #294 — bootstrap and migrations refactor (owns everything inside
  `docker/Dockerfile.meho`, the Alembic tree, and the migration entrypoint)
- [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) — format
  expectation for release-hygiene content
