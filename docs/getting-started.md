# Getting started with MEHO

> 15 minutes from a clean machine to the first investigation. If anything in this guide doesn't match what you see, the docs are wrong -- file a bug.

This page is the onboarding walkthrough. For deep architecture and deployment options, jump to:

- [docs/deployment.md](deployment.md) -- the full operator runbook (env vars, ports, production considerations, rescue script).
- [docs/codebase/bootstrap-and-migrations.md](codebase/bootstrap-and-migrations.md) -- how the bootstrap actually works under the hood.
- [docs/contributing/migrations.md](contributing/migrations.md) -- how to author a new database migration.

## What you need before starting (2 minutes)

| Tool | Why | How to check |
|---|---|---|
| **Docker Desktop 24+** with Compose v2 | runs the entire stack | `docker --version && docker compose version` |
| **Git** | clones the repo | `git --version` |
| **An LLM API key** | the agent needs a model | Anthropic Claude (recommended), OpenAI, or local Ollama |

You do **not** need Python, Node.js, or `uv` installed for the basic walkthrough. They are only required for the [hot-reload mode](#hot-reload-development-mode) below.

On Apple Silicon, also enable **Use Rosetta for x86_64/amd64 emulation** in Docker Desktop (Settings → General). The Voyage embeddings path skips Rosetta entirely; the local TEI fallback needs it.

## Step 1 -- clone and configure (3 minutes)

```bash
git clone https://github.com/evoila/meho.git
cd meho
cp env.example .env
```

Open `.env` in your editor and fill in three required values:

```bash
ANTHROPIC_API_KEY=sk-ant-...                      # or OPENAI_API_KEY / OLLAMA_BASE_URL
VOYAGE_API_KEY=...                                # optional, but the fast first-run path
CREDENTIAL_ENCRYPTION_KEY=                        # generate with the helper below
```

Generate the encryption key (Fernet symmetric, used to encrypt connector credentials at rest):

```bash
./scripts/generate-encryption-key.sh
# copy the printed value into CREDENTIAL_ENCRYPTION_KEY=
```

`docker compose` will refuse to start if `CREDENTIAL_ENCRYPTION_KEY` is empty -- this is intentional, the failure happens at compose-substitution time before any container is launched.

## Step 2 -- start the stack (5-10 minutes, mostly first-time image pulls)

```bash
docker compose up
```

Compose loads `docker-compose.yml` and the auto-loaded `docker-compose.override.yml` together. The override switches the `meho` image to its `debug` build target and exposes the debugpy port (5678). To run the production target locally without the debugger overhead, opt out explicitly with `docker compose -f docker-compose.yml up`.

What happens during startup, in order:

1. **PostgreSQL** boots with `pgvector` and runs `scripts/init-db.sql` to create the `keycloak` and `keycloak_test` databases (first boot only).
2. **Redis Stack**, **MinIO**, and **Seq** start.
3. **Keycloak** boots and imports the dev realm from `config/keycloak/`. This takes 60-90s on first run; the health check has a 90s start period.
4. **`meho` container** runs `scripts/run-migrations-monolith.sh` which executes `alembic -c meho_app/alembic.ini upgrade head` against the unified migration tree. Non-zero exit aborts startup.
5. **`meho` container** then starts the FastAPI app. The lifespan `_ensure_schema_ready` helper double-checks the `alembic_version` row matches the wheel's known head. If it doesn't, the app `SystemExit`s with the exact `alembic upgrade head` command to run -- this is the schema-readiness gate (#313).
6. **`meho-frontend`** builds and serves Vite on port 5173.

When startup is done you'll see the Vite ready banner in the logs. Open [http://localhost:5173](http://localhost:5173) and log in with the dev Keycloak credentials (default: `admin` / `admin`).

## Step 3 -- verify the stack is healthy (2 minutes)

```bash
curl -sf http://localhost:8000/health
# {"status":"ok"}
```

The full service map:

| Service | URL | Credentials |
|---|---|---|
| Frontend | [http://localhost:5173](http://localhost:5173) | Keycloak login |
| Backend API | [http://localhost:8000](http://localhost:8000) | JWT required |
| API docs (Swagger) | [http://localhost:8000/docs](http://localhost:8000/docs) | -- |
| Keycloak admin | [http://localhost:8080](http://localhost:8080) | admin / admin |
| MinIO console | [http://localhost:9001](http://localhost:9001) | minioadmin / minioadmin |
| Seq (logs/traces) | [http://localhost:5341](http://localhost:5341) | -- |
| PostgreSQL | localhost:5432 | meho / password |

## Step 4 -- run your first investigation

The frontend lands you on the chat view. Two modes are available:

- **Ask mode** -- knowledge-base Q&A with hybrid search + reranking. Drop a PDF runbook into the knowledge base and ask "what does this runbook say to do when an etcd node is unreachable?" to see it work.
- **Agent mode** -- cross-system ReAct investigation. Configure a connector (Kubernetes, Prometheus, GitHub, etc.) under Settings → Connectors, then ask "why is service `payments-api` returning 5xx?" The agent will plan, call connector operations, traverse the topology graph, and stitch together an explanation.

For a guided demo without configuring a real connector, the seeded REST connector against the public PokeAPI in `tests/fixtures/` works as a sanity check.

## Hot-reload development mode

Instead of rebuilding the `meho` image on every Python change, run the infrastructure in Docker and the application processes on the host. This is the default day-to-day development loop.

```bash
meho-dev local
```

What `meho-dev local` does:

- Starts only the infra services (Postgres, Redis, MinIO, Keycloak, Seq) in Docker.
- Runs `alembic upgrade head` against `localhost:5432`.
- Starts the backend with `uvicorn meho_app.main:app --reload` -- hot-reload on Python changes.
- Starts the frontend with `npm run dev` -- hot-reload on TypeScript/React changes.
- Forwards log output to your terminal; Ctrl-C cleanly stops both processes.

`meho-dev` is the Typer CLI introduced in Goal #294 (#310). It ships with the wheel via the `[project.scripts]` entry in `pyproject.toml`. The previous `scripts/dev-env.sh` is now a one-line shim that calls `uv run meho-dev`, so existing automation and muscle memory continue to work.

Other commands worth knowing:

```bash
meho-dev up           # full Docker mode (parity with `docker compose up` plus migrations + Keycloak setup)
meho-dev down         # stop everything (pass --volumes to wipe data)
meho-dev logs meho    # tail backend logs only
meho-dev status       # docker compose ps with health column
meho-dev test         # smoke + contract tests inside the meho container
meho-dev test-all     # full pytest suite inside the meho container
meho-dev validate     # run scripts/validate-services.sh against the running stack
```

Run `meho-dev --help` for the full list. Each subcommand has its own `--help`.

### Local-mode prerequisites

`meho-dev local` runs Python and Node on your host, so install:

- **Python 3.13+** with [`uv`](https://docs.astral.sh/uv/) -- `uv sync --group dev` will create the venv and install backend dev dependencies.
- **Node.js 20+** -- `cd meho_frontend && npm install` for the frontend dev dependencies.

`uv` manages the Python toolchain and the venv. You don't need to activate the venv -- everything is run via `uv run`.

## Troubleshooting

This section covers the failures you hit during onboarding. For per-feature issues (Kubernetes connector won't connect, etc.) see [docs/troubleshooting.md](troubleshooting.md).

### `docker compose up` exits immediately with `CREDENTIAL_ENCRYPTION_KEY: required`

You forgot Step 1 -- the `.env` file does not have a value for that variable. Run `./scripts/generate-encryption-key.sh` and paste the output into `.env`.

### `meho` container restarts in a loop with `SystemExit: schema mismatch`

The lifespan gate is doing its job: the `alembic_version` row in your database does not match the head revision the wheel knows about. Two cases:

1. **Fresh install where migrations failed.** Look at the logs from `scripts/run-migrations-monolith.sh` above the schema-mismatch message -- the underlying `alembic upgrade head` error will be there. Fix that error and re-run `docker compose up`.
2. **Pre-Goal-#294 deployment.** You have nine `alembic_version_meho_*` tables and no unified `alembic_version`. Run the rescue script once before starting the new container:
   ```bash
   docker compose stop meho meho-frontend
   DATABASE_URL=postgresql://meho:password@localhost:5432/meho \
     uv run python scripts/migrate_to_unified_alembic.py
   docker compose up
   ```
   Full procedure: [docs/deployment.md#upgrading-existing-deployments-rescue-script](deployment.md#upgrading-existing-deployments-rescue-script).

### Keycloak takes forever and the backend is `unhealthy`

Keycloak's first boot can take 60-90s on a normal machine and longer on Docker Desktop with constrained resources. The compose health check waits 90s. If you consistently see Keycloak time out:

- macOS / Windows: Docker Desktop → Settings → Resources → Memory → set to 6 GB+. The default 2 GB is not enough for the whole stack.
- Wait for `keycloak` to log `Keycloak ... started` before judging the backend.

### TEI embeddings container OOMs or hangs on Apple Silicon

The local TEI fallback runs under Rosetta on arm64 and is slow. If you have a Voyage AI account, set `VOYAGE_API_KEY` in `.env` and run plain `docker compose up` -- the TEI profile won't auto-activate, the stack stays light, and embeddings work natively. See [docs/troubleshooting.md#arm64--apple-silicon-first-run-issues](troubleshooting.md#arm64--apple-silicon-first-run-issues) for measured numbers.

### The frontend loads but every API call returns 401

You're not authenticated. The frontend redirects to Keycloak; complete the login flow with `admin` / `admin`. If Keycloak itself isn't reachable at [http://localhost:8080](http://localhost:8080), check the Keycloak container is healthy (`meho-dev status`) and that nothing else is bound to port 8080.

### `pre-commit` hooks fail with "env-example-sync"

The `scripts/check-env-example-sync.py` hook (#305) verifies `env.example` mirrors the Pydantic Settings classes. If a hook run reports MISSING_FROM_EXAMPLE or ORPHAN_IN_EXAMPLE, edit `env.example` to match the settings. Don't bypass the hook with `--no-verify`; CI runs the same check and will reject the PR.

## Where to go next

You're up and running. Next reading depends on what you're doing:

- **Authoring a database migration?** [docs/contributing/migrations.md](contributing/migrations.md) is the one-pager.
- **Adding a connector?** Follow the 16-step checklist in [AGENTS.md](https://github.com/evoila/meho/blob/main/AGENTS.md) and the full walkthrough at [docs/architecture/adding-connector.md](architecture/adding-connector.md).
- **Understanding the agent loop?** [docs/architecture/overview.md](architecture/overview.md) covers the ReAct graph, skills, and tool dispatch.
- **Operating MEHO in production?** [docs/deployment.md](deployment.md) has the production checklist (secrets, HTTPS, persistent storage, Keycloak hardening, scaling).

For unresolved issues, open a [GitHub Discussion](https://github.com/evoila/meho/discussions) or check existing issues at [github.com/evoila/meho/issues](https://github.com/evoila/meho/issues).
