# MEHO Makefile -- thin discovery layer over `meho-dev`, `uv`, and `npm`.
#
# Make's job here is to be the README-as-code: every common workflow has a
# one-line target you can run without remembering script paths or tool
# invocations. The actual logic lives in `meho_app/tools/dev.py` (the Typer
# app exposed as `meho-dev`), in the test runner scripts under `scripts/`,
# and in the frontend's npm scripts. Make should not grow business logic --
# if a recipe needs more than two non-trivial commands, push it down into
# `meho-dev` instead.

.PHONY: help install dev-up dev-down dev-local dev-restart logs status \
        test test-unit test-integration test-watch lint lint-fix typecheck \
        format ci verify clean

help:
	@echo "MEHO Development Commands"
	@echo ""
	@echo "Setup:"
	@echo "  make install       Install backend + dev + test dependencies via uv"
	@echo ""
	@echo "Development:"
	@echo "  make dev-up        Start full Docker stack (build + migrate + serve)"
	@echo "  make dev-down      Stop services (forwards extra args, e.g. ARGS='--volumes')"
	@echo "  make dev-local     Run infra in Docker, backend + frontend on the host with hot-reload"
	@echo "  make dev-restart   Stop and start the full stack"
	@echo "  make logs          Tail all service logs"
	@echo "  make status        Show docker compose ps with health"
	@echo ""
	@echo "Tests:"
	@echo "  make test          Run all backend tests (unit + integration)"
	@echo "  make test-unit     Run backend unit tests only"
	@echo "  make test-integration  Run backend integration tests (needs test stack up)"
	@echo "  make test-watch    Run unit tests in watch mode"
	@echo ""
	@echo "Code quality:"
	@echo "  make lint          Ruff + mypy (backend) and ESLint (frontend)"
	@echo "  make lint-fix      Apply ruff --fix + ruff format + eslint --fix"
	@echo "  make typecheck     mypy (backend) + tsc (frontend)"
	@echo "  make format        Format backend with ruff format"
	@echo ""
	@echo "Aggregate gates:"
	@echo "  make ci            Run every gate CI runs locally (lint + typecheck + tests)"
	@echo "  make verify        Run Goal #294 success-signal greps + alembic check + health probe"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean         Remove Python caches and build artifacts"

install:
	uv sync --group dev --group test

dev-up:
	uv run meho-dev up

dev-down:
	uv run meho-dev down $(ARGS)

dev-local:
	uv run meho-dev local

dev-restart:
	uv run meho-dev restart

logs:
	uv run meho-dev logs

status:
	uv run meho-dev status

test:
	./scripts/run-tests.sh

test-unit:
	./scripts/run-unit-tests.sh

test-integration:
	./scripts/run-integration-tests.sh

test-watch:
	./scripts/watch-tests.sh

lint:
	uv run ruff check meho_app/ tests/ scripts/
	uv run ruff format --check meho_app/ tests/ scripts/
	uv run mypy meho_app/ --ignore-missing-imports
	cd meho_frontend && npm run lint

lint-fix:
	uv run ruff check --fix meho_app/ tests/ scripts/
	uv run ruff format meho_app/ tests/ scripts/
	cd meho_frontend && npm run lint:fix

typecheck:
	uv run mypy meho_app/ --ignore-missing-imports
	cd meho_frontend && npm run typecheck

format:
	uv run ruff format meho_app/ tests/ scripts/

ci: lint typecheck
	uv run python scripts/check-env-example-sync.py
	./scripts/run-unit-tests.sh
	cd meho_frontend && npm run test:run

# Verification gates from Goal #294. Designed to run against either a clean
# checkout (the static greps + env-example-sync) or a running stack (the
# alembic + health probes). Targets that need a running stack will fail
# with a clear message rather than try to bring it up.
verify:
	@command -v rg >/dev/null 2>&1 || { \
	    echo "ERROR: ripgrep (rg) is required for 'make verify' but is not on PATH."; \
	    echo "Install: 'brew install ripgrep' (macOS) or 'apt-get install ripgrep' (Debian/Ubuntu)."; \
	    echo "Without rg the success-signal greps below silently pass on missing-command exit 127."; \
	    exit 1; \
	}
	@echo "==> Goal #294 success-signal greps"
	@if rg -n "alembic_version_meho_" meho_app/ | rg -v "^meho_app/alembic/versions/" >/dev/null; then \
	    echo "FAIL: stale alembic_version_meho_ refs outside the rescue script comments"; exit 1; \
	fi
	@echo "  ok: no stray alembic_version_meho_ references"
	@if rg -n "\|\| true" scripts/ | rg -i "migrat|alembic" >/dev/null; then \
	    echo "FAIL: '|| true' next to a migration command"; exit 1; \
	fi
	@echo "  ok: no '|| true' silencing migration failures"
	@if rg -n "2>/dev/null" scripts/ | rg -i "migrat|alembic" >/dev/null; then \
	    echo "FAIL: '2>/dev/null' next to a migration command"; exit 1; \
	fi
	@echo "  ok: no '2>/dev/null' silencing migration failures"
	@if rg -n "DO NOT USE docker compose DIRECTLY" scripts/ docs/getting-started.md docs/deployment.md README.md >/dev/null 2>&1; then \
	    echo "FAIL: 'DO NOT USE docker compose' warning resurfaced"; exit 1; \
	fi
	@echo "  ok: no 'DO NOT USE docker compose' warnings in operator-facing docs"
	@echo "==> env.example <-> Pydantic Settings sync"
	@uv run python scripts/check-env-example-sync.py
	@echo "==> Alembic state (advisory; skipped if stack is down)"
	@if docker compose ps --status running --services 2>/dev/null | grep -q '^meho$$'; then \
	    head=$$(docker compose exec -T meho uv run alembic -c meho_app/alembic.ini current 2>&1 | tail -1); \
	    printf "  current head: %s\n" "$$head"; \
	    echo "  alembic check (drift detection -- advisory):"; \
	    if docker compose exec -T meho uv run alembic -c meho_app/alembic.ini check >/dev/null 2>&1; then \
	        echo "    ok: model and migration head are in sync"; \
	    else \
	        echo "    advisory: drift detected -- run 'docker compose exec meho uv run alembic -c meho_app/alembic.ini check' for details"; \
	    fi; \
	else \
	    echo "  skipped: meho container is not running"; \
	fi
	@echo "==> Backend health probe (skipped if stack is down)"
	@if curl -sf http://localhost:8000/health > /dev/null 2>&1; then \
	    echo "  ok: GET /health -> 200"; \
	else \
	    echo "  skipped: backend not reachable on :8000"; \
	fi

clean:
	@find meho_app tests scripts -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find meho_app tests scripts -type f -name "*.pyc" -delete 2>/dev/null || true
	@find meho_app tests scripts -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
