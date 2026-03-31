"""Workflow template Pydantic model."""

from pathlib import Path

from pydantic import BaseModel, Field


class WorkflowTemplate(BaseModel):
    """Parsed workflow template with optional YAML frontmatter.

    Workflow templates are structured markdown files that guide Claude through
    multi-step operations (diagnosis, health-check, comparison, etc.). They use
    YAML frontmatter for metadata and markdown body for instructions.
    """

    name: str
    description: str = ""
    budget: int = Field(default=15, ge=1, le=100)
    version: str = ""
    content: str  # Full markdown content (including frontmatter)
    raw_body: str  # Content after frontmatter
    source_path: Path
    has_frontmatter: bool = True
    tags: list[str] = Field(default_factory=list)
