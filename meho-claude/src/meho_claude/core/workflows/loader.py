"""Workflow template parsing, listing, loading, and auto-install.

Discovers workflow templates from a directory, parses YAML frontmatter when
present, and provides functions to list, load, and auto-install bundled templates.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from meho_claude.core.workflows.models import WorkflowTemplate

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_workflow(path: Path) -> WorkflowTemplate:
    """Parse a workflow template file.

    Reads the file, extracts YAML frontmatter if present, and constructs
    a WorkflowTemplate model. Files without frontmatter get name=stem
    and has_frontmatter=False.

    Args:
        path: Path to the markdown workflow template file.

    Returns:
        Parsed WorkflowTemplate instance.
    """
    text = path.read_text()
    match = _FRONTMATTER_RE.match(text)

    if match:
        frontmatter = yaml.safe_load(match.group(1)) or {}
        body = text[match.end():]
        return WorkflowTemplate(
            name=frontmatter.get("name", path.stem),
            description=frontmatter.get("description", ""),
            budget=frontmatter.get("budget", 15),
            version=str(frontmatter.get("version", "")),
            content=text,
            raw_body=body,
            source_path=path,
            has_frontmatter=True,
            tags=frontmatter.get("tags", []),
        )
    else:
        return WorkflowTemplate(
            name=path.stem,
            content=text,
            raw_body=text,
            source_path=path,
            has_frontmatter=False,
        )


def list_workflows(workflows_dir: Path) -> list[WorkflowTemplate]:
    """Discover and parse all workflow templates in a directory.

    Globs for *.md files, skips files starting with underscore (like
    _template.md), and returns templates sorted by name.

    Args:
        workflows_dir: Directory containing workflow template markdown files.

    Returns:
        Sorted list of parsed WorkflowTemplate instances.
    """
    templates = []
    if workflows_dir.exists():
        for md_file in sorted(workflows_dir.glob("*.md")):
            if md_file.name.startswith("_"):
                continue
            templates.append(parse_workflow(md_file))
    return sorted(templates, key=lambda t: t.name)


def load_workflow(workflows_dir: Path, name: str) -> WorkflowTemplate | None:
    """Load a workflow template by name.

    First searches by frontmatter name match, then falls back to filename
    match (name + ".md").

    Args:
        workflows_dir: Directory containing workflow template markdown files.
        name: Workflow name to search for.

    Returns:
        Matching WorkflowTemplate, or None if not found.
    """
    # Search all templates (including underscore-prefixed) by frontmatter name
    if workflows_dir.exists():
        for md_file in sorted(workflows_dir.glob("*.md")):
            template = parse_workflow(md_file)
            if template.name == name:
                return template

    # Fallback: try exact filename match
    candidate = workflows_dir / f"{name}.md"
    if candidate.exists():
        return parse_workflow(candidate)

    return None


def ensure_bundled_workflows(
    workflows_dir: Path, force: bool = False
) -> list[str]:
    """Copy bundled workflow templates to the target directory.

    Uses importlib.resources to read templates from the meho_claude.data.workflows
    package and copies them to the specified directory. Does not overwrite
    existing files unless force=True.

    Args:
        workflows_dir: Target directory (typically ~/.meho/workflows/).
        force: If True, overwrite existing files.

    Returns:
        List of filenames that were copied.
    """
    import importlib.resources

    workflows_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    package = importlib.resources.files("meho_claude.data.workflows")

    for resource in package.iterdir():
        if not resource.name.endswith(".md"):
            continue

        target = workflows_dir / resource.name
        if target.exists() and not force:
            continue

        target.write_text(resource.read_text(encoding="utf-8"))
        copied.append(resource.name)

    return copied
