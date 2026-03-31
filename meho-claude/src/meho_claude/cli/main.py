"""Root Typer app with subcommand composition and global flags."""

import sys
import time
from types import SimpleNamespace

import typer

from meho_claude.cli import connector, topology, memory, knowledge, data, workflow

app = typer.Typer(
    name="meho",
    help="MEHO for Claude - Terminal DevOps Intelligence Engine",
    no_args_is_help=False,
)

# Register subcommand groups
app.add_typer(connector.app, name="connector")
app.add_typer(topology.app, name="topology")
app.add_typer(memory.app, name="memory")
app.add_typer(knowledge.app, name="knowledge")
app.add_typer(data.app, name="data")
app.add_typer(workflow.app, name="workflow")


def _ensure_initialized(ctx: typer.Context) -> None:
    """Lazily initialize heavy dependencies on first access.

    Returns immediately if already initialized (idempotent).
    Heavy imports (MehoSettings, database init, logging) are deferred
    here so that ``meho --help`` completes without them.
    """
    if ctx.obj.settings is not None:
        return

    from meho_claude.core.config import MehoSettings
    from meho_claude.core.database import initialize_databases
    from meho_claude.core.logging import configure_logging
    from meho_claude.core.state import ensure_state_dir

    settings = MehoSettings()

    # Override debug from CLI flag
    if ctx.obj.debug:
        settings.debug = True

    # Initialize state directory, databases, and logging
    ensure_state_dir(settings.state_dir)
    initialize_databases(settings.state_dir)
    configure_logging(settings.state_dir, settings.log_level, settings.debug)

    ctx.obj.settings = settings


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    human: bool = typer.Option(False, "--human", help="Human-readable Rich output."),
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
) -> None:
    """MEHO for Claude - Terminal DevOps Intelligence Engine."""
    # Store lightweight flags only -- heavy init deferred to _ensure_initialized
    ctx.obj = SimpleNamespace(human=human, debug=debug, settings=None, start_time=time.time())

    if ctx.invoked_subcommand is None:
        _ensure_initialized(ctx)
        _show_status(ctx)


def _show_status(ctx: typer.Context) -> None:
    """Quick status display for bare `meho` command."""
    from meho_claude.core.state import get_status_summary
    from meho_claude.cli.output import output_response

    status = get_status_summary(ctx.obj.settings.state_dir)
    output_response(status, human=ctx.obj.human, start_time=ctx.obj.start_time)


# ---------------------------------------------------------------------------
# meho init -- test-mockable seams
# ---------------------------------------------------------------------------


def _get_init_settings(ctx: typer.Context):
    """Get MehoSettings from Typer context (test-mockable seam)."""
    _ensure_initialized(ctx)
    return ctx.obj.settings


def _get_init_skills_dir() -> "Path":
    """Return the skills directory path (test-mockable seam)."""
    from pathlib import Path

    return Path.cwd() / ".claude" / "skills"


def _write_skills(skills_dir: "Path", force: bool = False) -> list[dict]:
    """Write MEHO skills (test-mockable seam)."""
    from meho_claude.core.skills import write_meho_skills

    return write_meho_skills(skills_dir, force=force)


def _ensure_workflows(workflows_dir: "Path", force: bool = False) -> list[str]:
    """Ensure bundled workflows are installed (test-mockable seam)."""
    from meho_claude.core.workflows.loader import ensure_bundled_workflows

    return ensure_bundled_workflows(workflows_dir, force=force)


@app.command()
def init(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="Overwrite existing skill files."),
) -> None:
    """Initialize MEHO skills in the current project (.claude/skills/)."""
    from meho_claude.cli.output import output_response

    start_time = time.time()

    settings = _get_init_settings(ctx)
    skills_dir = _get_init_skills_dir()
    results = _write_skills(skills_dir, force=force)

    workflows_dir = settings.state_dir / "workflows"
    installed = _ensure_workflows(workflows_dir, force=force)

    output_response(
        {
            "status": "ok",
            "skills_dir": str(skills_dir),
            "skills": results,
            "workflows_installed": installed,
        },
        human=ctx.obj.human,
        start_time=start_time,
    )


# ---------------------------------------------------------------------------
# Catch-all exception handler
# ---------------------------------------------------------------------------

# Wrap the Typer app __call__ to intercept unhandled exceptions and produce
# structured JSON error output instead of raw tracebacks.

_original_call = app.__call__


def _safe_call(*args, **kwargs):
    """Catch unexpected exceptions and emit structured error JSON."""
    try:
        return _original_call(*args, **kwargs)
    except SystemExit:
        raise  # Allow normal exit codes through
    except Exception as exc:
        from meho_claude.cli.output import output_error

        output_error(
            str(exc),
            code="INTERNAL_ERROR",
            suggestion="This is an unexpected error. Please report it with the full command you ran.",
        )


app.__call__ = _safe_call  # type: ignore[method-assign]
