# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
``meho-dev`` -- the development environment CLI (Goal #294 / #310).

Replaces ``scripts/dev-env.sh`` (~580 lines of bash) with a Typer app. Each
subcommand is a small Python function (see acceptance criteria in #310: each
under ~50 lines, helpers extracted) so that:

* ``meho-dev --help`` and ``meho-dev <cmd> --help`` produce structured help
  with typed options;
* dispatch is unit-testable -- ``CliRunner.invoke(app, [...])`` exercises
  argument parsing without touching docker (see
  ``tests/unit/tools/test_dev.py``);
* the bash wrapper at ``scripts/dev-env.sh`` shrinks to a one-line shim that
  delegates to ``meho-dev`` so existing references in docs, the Makefile,
  ``preflight.sh``, ``validate-install.sh``, etc. keep working unchanged.

Subcommand parity with the bash original (``up``, ``local``, ``down``,
``restart``, ``logs``, ``status``, ``validate``, ``test``, ``test-all``) is
preserved by design. Doc reconciliation -- replacing ``./scripts/dev-env.sh
<cmd>`` with ``meho-dev <cmd>`` across READMEs and per-test-suite docs -- is
deferred to Phase 7 PR-N.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

app = typer.Typer(
    name="meho-dev",
    help="MEHO development environment helper.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
ENV_FILE = REPO_ROOT / ".env"
ALEMBIC_INI = REPO_ROOT / "meho_app" / "alembic.ini"
PROJECT_NAME = "meho"


def _needs_tei_profile() -> bool:
    """TEI sidecars run only when ``VOYAGE_API_KEY`` is unset everywhere."""
    if os.environ.get("VOYAGE_API_KEY"):
        return False
    if not ENV_FILE.exists():
        return True
    for line in ENV_FILE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("VOYAGE_API_KEY="):
            return not stripped.split("=", 1)[1].strip()
    return True


def _compose_args() -> list[str]:
    """Build the standard ``docker compose ...`` invocation prefix."""
    args = ["docker", "compose", "-p", PROJECT_NAME, "-f", str(COMPOSE_FILE)]
    if _needs_tei_profile():
        args.extend(["--profile", "tei"])
    return args


def _compose(*subargs: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    """Run ``docker compose ...`` in the repo root."""
    return subprocess.run(  # noqa: S603 -- args are static + repo-controlled
        [*_compose_args(), *subargs],
        cwd=REPO_ROOT,
        check=check,
        text=True,
    )


def _ensure_env_file() -> None:
    """Refuse to proceed without ``.env``. Mirrors the bash wrapper's check."""
    if ENV_FILE.exists():
        return
    console.print("[red]Missing .env file.[/red]")
    console.print("Copy env.example and update the values:")
    console.print("  cp env.example .env")
    raise typer.Exit(code=1)


def _wait_for_service(service: str, retries: int = 60, sleep_seconds: int = 2) -> None:
    """Block until a Compose service reports healthy/running, or fail loudly."""
    console.print(f"⏳ Waiting for [bold]{service}[/bold] to become healthy...")
    for _ in range(retries):
        ps = subprocess.run(  # noqa: S603
            [*_compose_args(), "ps", "-q", service],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPO_ROOT,
        )
        cid = ps.stdout.strip()
        if cid:
            inspect_argv = [  # noqa: S607 -- relies on docker on PATH (parity with dev-env.sh)
                "docker",
                "inspect",
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                cid,
            ]
            inspect = subprocess.run(  # noqa: S603
                inspect_argv,
                capture_output=True,
                text=True,
                check=False,
            )
            health = inspect.stdout.strip()
            if health in {"healthy", "running"}:
                console.print(f"✅ {service} is [green]{health}[/green]")
                return
        time.sleep(sleep_seconds)
    console.print(f"[red]❌ {service} failed to become healthy in time[/red]")
    raise typer.Exit(code=1)


def _run_migrations(mode: str = "docker") -> None:
    """
    Drive Alembic against the unified migration tree at ``meho_app/alembic``.

    ``mode`` mirrors ``run_migrations`` in the original bash wrapper:

    * ``docker`` -- exec inside the running ``meho`` container, using the
      ``DATABASE_URL`` baked into the compose file.
    * ``local`` -- run alembic on the host against ``localhost:5432``; used
      by ``meho-dev local`` because uvicorn runs outside docker.

    Both modes propagate non-zero exit codes (no ``2>/dev/null``, no
    ``|| true``).
    """
    console.print(f"\n[bold blue]🔄 Running database migrations ({mode})...[/bold blue]")
    if mode == "docker":
        result = _compose(
            "exec",
            "-T",
            "meho",
            "uv",
            "run",
            "alembic",
            "-c",
            "meho_app/alembic.ini",
            "upgrade",
            "head",
        )
    elif mode == "local":
        env = os.environ.copy()
        env["DATABASE_URL"] = "postgresql+asyncpg://meho:password@localhost:5432/meho"
        argv = [  # noqa: S607 -- relies on uv on PATH (mirrors dev-env.sh)
            "uv",
            "run",
            "alembic",
            "-c",
            str(ALEMBIC_INI),
            "upgrade",
            "head",
        ]
        result = subprocess.run(  # noqa: S603
            argv,
            cwd=REPO_ROOT,
            env=env,
            check=False,
            text=True,
        )
    else:
        console.print(f"[red]❌ unknown mode '{mode}' (expected 'docker' or 'local')[/red]")
        raise typer.Exit(code=2)
    if result.returncode != 0:
        console.print("[red]❌ Migration failed[/red]")
        raise typer.Exit(code=result.returncode)
    console.print("[green]✅ Migrations complete[/green]")


def _run_keycloak_setup() -> None:
    """Best-effort Keycloak bootstrap. Warnings here are non-fatal by design."""
    console.print("\n🔐 Configuring Keycloak...")
    script = REPO_ROOT / "scripts" / "setup-keycloak.sh"
    result = subprocess.run([str(script)], cwd=REPO_ROOT, check=False)  # noqa: S603
    if result.returncode != 0:
        console.print("[yellow]⚠️  Keycloak setup had warnings (see above)[/yellow]")


def _kill_local_processes() -> None:
    """``pkill`` by command-line pattern. Avoid ports to spare the docker daemon."""
    for pattern in (
        "uvicorn meho_app.main:app",
        "vite.*meho_frontend",
        "node.*meho_frontend",
    ):
        argv = ["pkill", "-f", pattern]  # noqa: S607 -- pkill on PATH (parity with dev-env.sh)
        subprocess.run(argv, check=False)  # noqa: S603


@app.command(name="up")
def cmd_up() -> None:
    """Build images, start all services in Docker, and run migrations."""
    _ensure_env_file()

    console.print("🔍 Running type checks before build...")
    typecheck = subprocess.run(  # noqa: S603
        [str(REPO_ROOT / "scripts" / "typecheck.sh"), "--quiet"],
        cwd=REPO_ROOT,
        check=False,
    )
    if typecheck.returncode != 0:
        console.print("[yellow]⚠️  Type errors detected[/yellow]")
        if not typer.confirm("Continue anyway?", default=False):
            console.print("[red]❌ Aborted. Fix type errors and try again[/red]")
            raise typer.Exit(code=1)
    else:
        console.print("[green]✅ Type checking passed[/green]\n")

    console.print("📦 Building and starting services...")
    if _compose("up", "-d", "--build").returncode != 0:
        raise typer.Exit(code=1)

    console.print("\n⏳ Waiting for infrastructure services...")
    _wait_for_service("postgres")
    _wait_for_service("redis")
    _wait_for_service("keycloak", retries=90)

    console.print("\n⏳ Waiting for MEHO backend...")
    # The container entrypoint (docker/docker-entrypoint.sh) runs migrations
    # before exec'ing uvicorn, and the lifespan schema-readiness gate refuses
    # to start until alembic_version is at head. So once meho is healthy,
    # migrations have necessarily already run -- a second alembic upgrade
    # here would be a no-op against an already-migrated DB and would
    # mislead operators into thinking migrations are post-boot work.
    _wait_for_service("meho", retries=150)

    _run_keycloak_setup()

    keycloak_pw = os.environ.get("KEYCLOAK_ADMIN_PASSWORD", "admin")
    console.print(
        "\n[green]========================================\n"
        "✅ MEHO is running!\n"
        "========================================[/green]\n\n"
        "Services:\n"
        "  • Backend API:    http://localhost:8000\n"
        "  • API Docs:       http://localhost:8000/docs\n"
        "  • Frontend:       http://localhost:5173 (if started)\n"
        f"  • Keycloak Admin: http://localhost:8080 (admin/{keycloak_pw})\n"
        "  • MinIO Console:  http://localhost:9001 (admin/minioadmin)\n"
        "  • Seq (Logs):     http://localhost:5341\n"
        "  • PostgreSQL:     localhost:5432 (meho/password)\n"
        "  • Redis:          localhost:6379\n\n"
        "Commands:\n"
        "  meho-dev logs       # tail logs\n"
        "  meho-dev down       # stop services\n"
        "  meho-dev test       # run critical tests\n"
    )


def _do_down(extra: list[str] | None = None) -> None:
    """Stop local processes and then ``docker compose down`` (with optional extras).

    Factored out of ``cmd_down`` so ``cmd_restart`` can compose it without
    needing to fabricate a ``typer.Context``. Raises ``typer.Exit`` on
    non-zero compose exit codes -- callers that want to keep going should
    catch it.
    """
    extras = extra or []
    console.print("[bold]🛑 Stopping services...[/bold]")
    console.print("  → stopping local backend (uvicorn) and frontend (vite)...")
    _kill_local_processes()
    time.sleep(1)
    console.print("  → stopping Docker containers...")
    result = _compose("down", *extras)
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)
    console.print("[green]✅ Services stopped[/green]")


@app.command(
    name="down",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def cmd_down(ctx: typer.Context) -> None:
    """
    Stop all services.

    Any extra arguments are forwarded verbatim to ``docker compose down``.
    The pre-#310 bash equivalent supported, e.g., ``./scripts/dev-env.sh
    down --volumes``; the Typer command keeps that contract by enabling
    ``ignore_unknown_options`` so flags like ``--volumes`` are not
    intercepted as Typer options.
    """
    _do_down(list(ctx.args))


@app.command(name="restart")
def cmd_restart() -> None:
    """``down`` followed by ``up``."""
    _do_down()
    console.print()
    cmd_up()


@app.command(name="logs")
def cmd_logs(
    services: Annotated[
        list[str] | None,
        typer.Argument(help="Optional service names to filter. Empty = all."),
    ] = None,
) -> None:
    """Tail logs (``docker compose logs -f [services...]``)."""
    result = _compose("logs", "-f", *(services or []))
    raise typer.Exit(code=result.returncode)


@app.command(name="status")
def cmd_status() -> None:
    """Show ``docker compose ps``."""
    raise typer.Exit(code=_compose("ps").returncode)


@app.command(name="validate")
def cmd_validate() -> None:
    """Run ``scripts/validate-services.sh`` against the running stack."""
    console.print("🔍 Validating services...")
    result = subprocess.run(  # noqa: S603
        [str(REPO_ROOT / "scripts" / "validate-services.sh")],
        cwd=REPO_ROOT,
        check=False,
    )
    raise typer.Exit(code=result.returncode)


@app.command(name="test")
def cmd_test() -> None:
    """Run critical tests (smoke + contract) inside the meho container."""
    console.print("🧪 Running critical tests (smoke + contract)...")
    result = _compose(
        "exec",
        "-T",
        "meho",
        "bash",
        "-c",
        "cd /app && ./scripts/run-critical-tests.sh --fast",
    )
    raise typer.Exit(code=result.returncode)


@app.command(name="test-all")
def cmd_test_all() -> None:
    """Run the full pytest suite inside the meho container."""
    console.print("🧪 Running all tests...")
    result = _compose("exec", "-T", "meho", "bash", "-c", "cd /app && pytest tests/")
    raise typer.Exit(code=result.returncode)


def _local_overrides() -> dict[str, str]:
    """
    Process-env overrides for ``meho-dev local``.

    The container hostnames in ``docker-compose.yml`` are unreachable from
    the host -- uvicorn runs outside docker in this mode, so we point at the
    container ports as published on ``localhost``. Pydantic Settings honours
    process env over ``.env``, so these wins.
    """
    return {
        "DATABASE_URL": "postgresql+asyncpg://meho:password@localhost:5432/meho",
        "REDIS_URL": "redis://localhost:6379/0",
        "OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
        "OBJECT_STORAGE_ACCESS_KEY": os.environ.get("OBJECT_STORAGE_ACCESS_KEY", "minioadmin"),
        "OBJECT_STORAGE_SECRET_KEY": os.environ.get("OBJECT_STORAGE_SECRET_KEY", "minioadmin"),
        "OBJECT_STORAGE_BUCKET": os.environ.get("OBJECT_STORAGE_BUCKET", "meho-dev-data"),
        "OBJECT_STORAGE_USE_SSL": os.environ.get("OBJECT_STORAGE_USE_SSL", "false"),
        "ENV": os.environ.get("ENV", "dev"),
        "KEYCLOAK_URL": os.environ.get("KEYCLOAK_URL", "http://localhost:8080"),
        "KEYCLOAK_CLIENT_ID": os.environ.get("KEYCLOAK_CLIENT_ID", "meho-api"),
        "OTEL_SERVICE_NAME": os.environ.get("OTEL_SERVICE_NAME", "meho"),
        "OTEL_EXPORTER_OTLP_ENDPOINT": os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:5341/ingest/otlp"
        ),
        "OTEL_CONSOLE": os.environ.get("OTEL_CONSOLE", "true"),
        "MEHO_LOG_LEVEL": os.environ.get("MEHO_LOG_LEVEL", "INFO"),
    }


def _start_local_backend(env: dict[str, str]) -> subprocess.Popen[bytes]:
    """
    Spawn ``uv run uvicorn meho_app.main:app --reload`` and return the handle.

    Mirrors ``scripts/dev-env.sh`` line-for-line: ``--host 0.0.0.0`` exposes
    the dev server on the host's loopback so the frontend dev server (port
    5173) can reach the backend on port 8000. Production binds are handled
    by the compose file, not this CLI.
    """
    backend_argv = [
        "uv",
        "run",
        "uvicorn",
        "meho_app.main:app",
        "--reload",
        "--host",
        "0.0.0.0",  # noqa: S104 -- dev-only bind, parity with dev-env.sh
        "--port",
        "8000",
    ]
    console.print("[blue]Starting backend...[/blue]")
    return subprocess.Popen(backend_argv, cwd=REPO_ROOT, env=env)  # noqa: S603, S607


def _start_local_frontend() -> subprocess.Popen[bytes] | None:
    """Spawn ``npm run dev`` from ``meho_frontend/``. None when npm is missing."""
    if shutil.which("npm") is None:
        console.print(
            "[yellow]npm not found -- skipping frontend (install Node 20+ to enable).[/yellow]"
        )
        return None
    console.print("[blue]Starting frontend...[/blue]")
    frontend_argv = ["npm", "run", "dev"]
    return subprocess.Popen(frontend_argv, cwd=REPO_ROOT / "meho_frontend")  # noqa: S603, S607


@app.command(name="local")
def cmd_local() -> None:
    """Run infra in Docker; backend + frontend on the host with hot reload."""
    _ensure_env_file()
    console.print("\n[bold blue]🚀 MEHO Local Development Mode[/bold blue]")

    _kill_local_processes()
    time.sleep(1)

    console.print("\n📦 Ensuring Docker app services are stopped...")
    _compose("stop", "meho", "meho-frontend")
    _compose("rm", "-f", "meho", "meho-frontend")

    console.print("\n📦 Starting infrastructure services...")
    if _compose("up", "-d", "postgres", "redis", "minio", "keycloak", "seq").returncode != 0:
        raise typer.Exit(code=1)

    _wait_for_service("postgres")
    _wait_for_service("redis")
    _wait_for_service("keycloak", retries=90)

    _run_migrations(mode="local")
    _run_keycloak_setup()

    env = {**os.environ, **_local_overrides()}
    backend = _start_local_backend(env)
    time.sleep(3)
    if backend.poll() is not None:
        console.print("[red]❌ Backend failed to start[/red]")
        raise typer.Exit(code=1)
    frontend = _start_local_frontend()

    console.print(
        "\n[green]========================================\n"
        "🎉 Hot-reload development ready!\n"
        "========================================[/green]\n\n"
        "  Backend:   http://localhost:8000\n"
        "  Frontend:  http://localhost:5173\n"
        "  API Docs:  http://localhost:8000/docs\n"
        "  Seq:       http://localhost:5341\n\n"
        "[yellow]Press Ctrl+C to stop[/yellow]\n"
    )

    def _shutdown(_signum: int, _frame: object) -> None:
        console.print("\n[yellow]Stopping services...[/yellow]")
        for proc, label in ((backend, "backend"), (frontend, "frontend")):
            if proc and proc.poll() is None:
                console.print(f"  → stopping {label}...")
                proc.send_signal(signal.SIGTERM)
        _kill_local_processes()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    backend.wait()
    if frontend is not None:
        frontend.wait()


if __name__ == "__main__":
    app()
