"""Tests for workflow template parsing, listing, loading, and auto-install."""

from pathlib import Path
from unittest.mock import patch

import pytest

from meho_claude.core.workflows.models import WorkflowTemplate
from meho_claude.core.workflows.loader import (
    parse_workflow,
    list_workflows,
    load_workflow,
    ensure_bundled_workflows,
)


# ---- WorkflowTemplate Model Tests ----


class TestWorkflowTemplateModel:
    def test_name_is_required(self):
        """WorkflowTemplate requires a name."""
        t = WorkflowTemplate(
            name="test",
            content="some content",
            raw_body="some content",
            source_path=Path("/tmp/test.md"),
        )
        assert t.name == "test"

    def test_budget_defaults_to_15(self):
        """Budget defaults to 15 when not provided."""
        t = WorkflowTemplate(
            name="test",
            content="content",
            raw_body="content",
            source_path=Path("/tmp/test.md"),
        )
        assert t.budget == 15

    def test_version_defaults_to_empty(self):
        """Version defaults to empty string."""
        t = WorkflowTemplate(
            name="test",
            content="content",
            raw_body="content",
            source_path=Path("/tmp/test.md"),
        )
        assert t.version == ""

    def test_description_defaults_to_empty(self):
        """Description defaults to empty string."""
        t = WorkflowTemplate(
            name="test",
            content="content",
            raw_body="content",
            source_path=Path("/tmp/test.md"),
        )
        assert t.description == ""

    def test_tags_defaults_to_empty_list(self):
        """Tags defaults to empty list."""
        t = WorkflowTemplate(
            name="test",
            content="content",
            raw_body="content",
            source_path=Path("/tmp/test.md"),
        )
        assert t.tags == []

    def test_has_frontmatter_defaults_to_true(self):
        """has_frontmatter defaults to True."""
        t = WorkflowTemplate(
            name="test",
            content="content",
            raw_body="content",
            source_path=Path("/tmp/test.md"),
        )
        assert t.has_frontmatter is True

    def test_budget_min_validation(self):
        """Budget must be >= 1."""
        with pytest.raises(Exception):
            WorkflowTemplate(
                name="test",
                content="content",
                raw_body="content",
                source_path=Path("/tmp/test.md"),
                budget=0,
            )

    def test_budget_max_validation(self):
        """Budget must be <= 100."""
        with pytest.raises(Exception):
            WorkflowTemplate(
                name="test",
                content="content",
                raw_body="content",
                source_path=Path("/tmp/test.md"),
                budget=101,
            )


# ---- parse_workflow Tests ----


class TestParseWorkflow:
    def test_parse_with_valid_frontmatter(self, tmp_path):
        """parse_workflow with valid frontmatter returns WorkflowTemplate with all fields."""
        md = tmp_path / "test.md"
        md.write_text(
            "---\n"
            "name: diagnose\n"
            "description: Cross-system diagnostic investigation\n"
            "budget: 15\n"
            'version: "1.0"\n'
            "---\n"
            "\n"
            "# Diagnosis Workflow\n"
            "\nSome content here.\n"
        )

        result = parse_workflow(md)

        assert result.name == "diagnose"
        assert result.description == "Cross-system diagnostic investigation"
        assert result.budget == 15
        assert result.version == "1.0"
        assert result.has_frontmatter is True
        assert "# Diagnosis Workflow" in result.raw_body
        assert "---" in result.content  # Full content includes frontmatter

    def test_parse_without_frontmatter(self, tmp_path):
        """parse_workflow without frontmatter returns name=stem, has_frontmatter=False."""
        md = tmp_path / "my-workflow.md"
        md.write_text("# My Custom Workflow\n\nJust some steps.\n")

        result = parse_workflow(md)

        assert result.name == "my-workflow"
        assert result.has_frontmatter is False
        assert result.raw_body == "# My Custom Workflow\n\nJust some steps.\n"
        assert result.budget == 15  # Default

    def test_parse_with_partial_frontmatter_missing_budget(self, tmp_path):
        """parse_workflow with partial frontmatter (missing budget) uses default budget=15."""
        md = tmp_path / "partial.md"
        md.write_text(
            "---\n"
            "name: partial\n"
            "description: No budget specified\n"
            "---\n"
            "\n"
            "Content here.\n"
        )

        result = parse_workflow(md)

        assert result.name == "partial"
        assert result.budget == 15  # Default
        assert result.description == "No budget specified"
        assert result.has_frontmatter is True

    def test_parse_with_tags(self, tmp_path):
        """parse_workflow extracts tags from frontmatter."""
        md = tmp_path / "tagged.md"
        md.write_text(
            "---\n"
            "name: tagged\n"
            "tags:\n"
            "  - diagnosis\n"
            "  - infrastructure\n"
            "---\n"
            "\nContent.\n"
        )

        result = parse_workflow(md)

        assert result.tags == ["diagnosis", "infrastructure"]


# ---- list_workflows Tests ----


class TestListWorkflows:
    def test_list_returns_sorted_by_name(self, tmp_path):
        """list_workflows returns templates sorted by name."""
        (tmp_path / "zebra.md").write_text(
            "---\nname: zebra\n---\n\nContent.\n"
        )
        (tmp_path / "alpha.md").write_text(
            "---\nname: alpha\n---\n\nContent.\n"
        )

        result = list_workflows(tmp_path)

        assert len(result) == 2
        assert result[0].name == "alpha"
        assert result[1].name == "zebra"

    def test_list_excludes_underscore_files(self, tmp_path):
        """list_workflows skips files starting with underscore."""
        (tmp_path / "_template.md").write_text("---\nname: template\n---\n\n")
        (tmp_path / "diagnose.md").write_text("---\nname: diagnose\n---\n\n")

        result = list_workflows(tmp_path)

        assert len(result) == 1
        assert result[0].name == "diagnose"

    def test_list_empty_directory(self, tmp_path):
        """list_workflows on empty directory returns empty list."""
        result = list_workflows(tmp_path)
        assert result == []

    def test_list_nonexistent_directory(self, tmp_path):
        """list_workflows on nonexistent directory returns empty list."""
        result = list_workflows(tmp_path / "nonexistent")
        assert result == []

    def test_list_includes_structured_and_unstructured(self, tmp_path):
        """list_workflows includes both structured and unstructured templates."""
        (tmp_path / "structured.md").write_text(
            "---\nname: structured\ndescription: Has frontmatter\n---\n\nContent.\n"
        )
        (tmp_path / "unstructured.md").write_text(
            "# Just a plain markdown file\n\nNo frontmatter here.\n"
        )

        result = list_workflows(tmp_path)

        assert len(result) == 2
        structured = [t for t in result if t.name == "structured"][0]
        unstructured = [t for t in result if t.name == "unstructured"][0]
        assert structured.has_frontmatter is True
        assert unstructured.has_frontmatter is False


# ---- load_workflow Tests ----


class TestLoadWorkflow:
    def test_load_by_name_returns_template(self, tmp_path):
        """load_workflow by name returns the matching template."""
        (tmp_path / "diagnose.md").write_text(
            "---\nname: diagnose\ndescription: Diagnostic\n---\n\nDiagnosis steps.\n"
        )

        result = load_workflow(tmp_path, "diagnose")

        assert result is not None
        assert result.name == "diagnose"
        assert "Diagnosis steps." in result.raw_body

    def test_load_unknown_name_returns_none(self, tmp_path):
        """load_workflow with unknown name returns None."""
        (tmp_path / "diagnose.md").write_text(
            "---\nname: diagnose\n---\n\n"
        )

        result = load_workflow(tmp_path, "nonexistent")

        assert result is None

    def test_load_by_filename_fallback(self, tmp_path):
        """load_workflow tries filename match if no frontmatter name match."""
        (tmp_path / "my-workflow.md").write_text(
            "# My Workflow\n\nNo frontmatter.\n"
        )

        result = load_workflow(tmp_path, "my-workflow")

        assert result is not None
        assert result.name == "my-workflow"


# ---- ensure_bundled_workflows Tests ----


class TestEnsureBundledWorkflows:
    def test_copies_bundled_templates_to_target(self, tmp_path):
        """ensure_bundled_workflows copies bundled templates to target dir."""
        copied = ensure_bundled_workflows(tmp_path)

        assert len(copied) > 0
        # Check that known bundled files were copied
        assert "diagnose.md" in copied
        assert "health-check.md" in copied
        assert "compare.md" in copied
        assert "_template.md" in copied

        # Verify files exist
        assert (tmp_path / "diagnose.md").exists()
        assert (tmp_path / "health-check.md").exists()

    def test_does_not_overwrite_existing(self, tmp_path):
        """ensure_bundled_workflows does not overwrite existing files."""
        # Pre-create a customized diagnose.md
        custom_content = "# My Custom Diagnose\n"
        (tmp_path / "diagnose.md").write_text(custom_content)

        copied = ensure_bundled_workflows(tmp_path)

        # diagnose.md should NOT be in copied list (was skipped)
        assert "diagnose.md" not in copied

        # Content should be unchanged
        assert (tmp_path / "diagnose.md").read_text() == custom_content

    def test_force_overwrites_existing(self, tmp_path):
        """ensure_bundled_workflows with force=True overwrites existing files."""
        # Pre-create a customized diagnose.md
        (tmp_path / "diagnose.md").write_text("# Custom\n")

        copied = ensure_bundled_workflows(tmp_path, force=True)

        # diagnose.md should be in copied list (was overwritten)
        assert "diagnose.md" in copied

        # Content should be the bundled version
        content = (tmp_path / "diagnose.md").read_text()
        assert "Cross-System Diagnosis" in content
