"""Workflow subcommand group -- manage and run workflow templates.

Provides `meho workflow list` and `meho workflow run <name>` commands
for discovering and outputting workflow templates that guide Claude through
structured multi-step operations (diagnosis, health-check, comparison, etc.).
"""

import time
from pathlib import Path

import typer

from meho_claude.cli.output import output_error, output_response

app = typer.Typer(help="Manage and run workflow templates.")


def _get_settings(ctx: typer.Context):
    """Get MehoSettings from Typer context."""
    from meho_claude.cli.main import _ensure_initialized

    _ensure_initialized(ctx)
    return ctx.obj.settings


def _list_workflows(workflows_dir: Path) -> list[dict]:
    """List workflow templates with parsed frontmatter (test-mockable seam).

    Auto-installs bundled templates on first access before listing.
    """
    from meho_claude.core.workflows.loader import ensure_bundled_workflows, list_workflows

    ensure_bundled_workflows(workflows_dir)
    result = []
    for t in list_workflows(workflows_dir):
        d = t.model_dump(exclude={"content", "raw_body"})
        d["source_path"] = str(d["source_path"])
        result.append(d)
    return result


def _load_workflow(workflows_dir: Path, name: str) -> str | None:
    """Load a workflow template by name (test-mockable seam).

    Auto-installs bundled templates on first access before loading.
    """
    from meho_claude.core.workflows.loader import ensure_bundled_workflows, load_workflow

    ensure_bundled_workflows(workflows_dir)
    template = load_workflow(workflows_dir, name)
    return template.content if template else None


@app.command("list")
def list_cmd(ctx: typer.Context) -> None:
    """List available workflow templates."""
    start_time = time.time()
    settings = _get_settings(ctx)
    workflows_dir = settings.state_dir / "workflows"

    workflows = _list_workflows(workflows_dir)

    output_response(
        {"status": "ok", "workflows": workflows, "count": len(workflows)},
        human=ctx.obj.human,
        start_time=start_time,
    )


@app.command()
def run(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Workflow template name."),
) -> None:
    """Output a workflow template for Claude to follow."""
    start_time = time.time()
    settings = _get_settings(ctx)
    workflows_dir = settings.state_dir / "workflows"

    content = _load_workflow(workflows_dir, name)
    if content is None:
        output_error(
            f"Workflow not found: {name}",
            code="WORKFLOW_NOT_FOUND",
            suggestion="Run 'meho workflow list' to see available workflows.",
        )
        return

    output_response(
        {"status": "ok", "workflow": name, "template": content},
        human=ctx.obj.human,
        start_time=start_time,
    )
