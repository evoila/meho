"""Tests for MEHO skill definitions and writer (meho init support).

Tests for get_meho_skill_definitions() and write_meho_skills() which generate
SKILL.md files for Claude Code slash command registration.
"""

from pathlib import Path

import pytest

from meho_claude.core.skills import get_meho_skill_definitions, write_meho_skills


# ---- get_meho_skill_definitions Tests ----


class TestGetMehoSkillDefinitions:
    def test_returns_five_skills(self):
        """Exactly 5 skill definitions returned."""
        definitions = get_meho_skill_definitions()
        assert len(definitions) == 5

    def test_returns_list_of_tuples(self):
        """Each definition is a (dir_name, content) tuple."""
        definitions = get_meho_skill_definitions()
        for item in definitions:
            assert isinstance(item, tuple)
            assert len(item) == 2
            assert isinstance(item[0], str)
            assert isinstance(item[1], str)

    def test_correct_directory_names(self):
        """Directory names match meho-diagnose, meho-connect, meho-topology, meho-knowledge, meho-memory."""
        definitions = get_meho_skill_definitions()
        dir_names = [d[0] for d in definitions]
        expected = {"meho-diagnose", "meho-connect", "meho-topology", "meho-knowledge", "meho-memory"}
        assert set(dir_names) == expected

    def test_each_skill_has_yaml_frontmatter(self):
        """Each skill content starts with --- (YAML frontmatter)."""
        definitions = get_meho_skill_definitions()
        for dir_name, content in definitions:
            assert content.startswith("---"), f"{dir_name} does not start with YAML frontmatter"
            # Must have closing ---
            lines = content.split("\n")
            closing_indices = [i for i, line in enumerate(lines) if line.strip() == "---" and i > 0]
            assert len(closing_indices) >= 1, f"{dir_name} has no closing --- for frontmatter"

    def test_each_skill_has_name_field(self):
        """Each skill frontmatter contains a name field."""
        definitions = get_meho_skill_definitions()
        for dir_name, content in definitions:
            assert "name:" in content, f"{dir_name} missing name field"

    def test_each_skill_has_description_field(self):
        """Each skill frontmatter contains a description field."""
        definitions = get_meho_skill_definitions()
        for dir_name, content in definitions:
            assert "description:" in content, f"{dir_name} missing description field"

    def test_each_skill_has_argument_hint_field(self):
        """Each skill frontmatter contains an argument-hint field."""
        definitions = get_meho_skill_definitions()
        for dir_name, content in definitions:
            assert "argument-hint:" in content, f"{dir_name} missing argument-hint field"


# ---- meho-diagnose content Tests ----


class TestDiagnoseSkillContent:
    @pytest.fixture
    def diagnose_content(self):
        definitions = get_meho_skill_definitions()
        return next(content for name, content in definitions if name == "meho-diagnose")

    def test_name_is_meho_colon_diagnose(self, diagnose_content):
        """Frontmatter name uses colon syntax: meho:diagnose."""
        assert "name: meho:diagnose" in diagnose_content

    def test_contains_workflow_run_reference(self, diagnose_content):
        """References meho workflow run diagnose for template injection."""
        assert "meho workflow run diagnose" in diagnose_content

    def test_contains_connector_list_reference(self, diagnose_content):
        """References meho connector list for live connector status."""
        assert "meho connector list" in diagnose_content

    def test_contains_read_only_rule(self, diagnose_content):
        """Enforces READ-only operations during diagnosis."""
        assert "READ" in diagnose_content
        # Must contain a rule about only executing READ operations
        lower = diagnose_content.lower()
        assert "only" in lower and "read" in lower

    def test_contains_arguments_variable(self, diagnose_content):
        """Contains $ARGUMENTS for user problem injection."""
        assert "$ARGUMENTS" in diagnose_content

    def test_contains_confidence_levels(self, diagnose_content):
        """Instructs Claude to include confidence levels."""
        assert "confidence" in diagnose_content.lower()

    def test_contains_same_as_traversal(self, diagnose_content):
        """Instructs Claude to follow SAME_AS edges across systems."""
        assert "SAME_AS" in diagnose_content

    def test_contains_narrate_instruction(self, diagnose_content):
        """Instructs Claude to narrate investigation steps."""
        lower = diagnose_content.lower()
        assert "narrate" in lower

    def test_contains_recommend_not_execute(self, diagnose_content):
        """Instructs Claude to recommend but NOT execute WRITE/DESTRUCTIVE actions."""
        assert "recommend" in diagnose_content.lower() or "Recommend" in diagnose_content
        assert "DO NOT execute" in diagnose_content or "do NOT execute" in diagnose_content or "WRITE" in diagnose_content

    def test_contains_topology_lookup(self, diagnose_content):
        """References meho topology lookup for entity resolution."""
        assert "meho topology lookup" in diagnose_content

    def test_contains_connector_call(self, diagnose_content):
        """References meho connector call for evidence gathering."""
        assert "meho connector call" in diagnose_content


# ---- meho-connect content Tests ----


class TestConnectSkillContent:
    @pytest.fixture
    def connect_content(self):
        definitions = get_meho_skill_definitions()
        return next(content for name, content in definitions if name == "meho-connect")

    def test_name_is_meho_colon_connect(self, connect_content):
        """Frontmatter name uses colon syntax: meho:connect."""
        assert "name: meho:connect" in connect_content

    def test_contains_connector_add(self, connect_content):
        """Wraps meho connector add command."""
        assert "meho connector add" in connect_content

    def test_contains_connector_types(self, connect_content):
        """Lists connector types."""
        assert "rest" in connect_content.lower()
        assert "kubernetes" in connect_content.lower()
        assert "vmware" in connect_content.lower()


# ---- meho-topology content Tests ----


class TestTopologySkillContent:
    @pytest.fixture
    def topology_content(self):
        definitions = get_meho_skill_definitions()
        return next(content for name, content in definitions if name == "meho-topology")

    def test_name_is_meho_colon_topology(self, topology_content):
        """Frontmatter name uses colon syntax: meho:topology."""
        assert "name: meho:topology" in topology_content

    def test_contains_topology_lookup(self, topology_content):
        """Wraps meho topology lookup command."""
        assert "meho topology lookup" in topology_content


# ---- meho-knowledge and meho-memory content Tests ----


class TestKnowledgeSkillContent:
    @pytest.fixture
    def knowledge_content(self):
        definitions = get_meho_skill_definitions()
        return next(content for name, content in definitions if name == "meho-knowledge")

    def test_knowledge_name(self, knowledge_content):
        assert "name: meho:knowledge" in knowledge_content

    def test_knowledge_has_ingest_command(self, knowledge_content):
        """Knowledge skill includes ingest command."""
        assert "meho knowledge ingest" in knowledge_content

    def test_knowledge_has_search_command(self, knowledge_content):
        """Knowledge skill includes search command."""
        assert "meho knowledge search" in knowledge_content

    def test_knowledge_has_remove_command(self, knowledge_content):
        """Knowledge skill includes remove command."""
        assert "meho knowledge remove" in knowledge_content

    def test_knowledge_has_rebuild_command(self, knowledge_content):
        """Knowledge skill includes rebuild command."""
        assert "meho knowledge rebuild" in knowledge_content

    def test_knowledge_has_stats_command(self, knowledge_content):
        """Knowledge skill includes stats command."""
        assert "meho knowledge stats" in knowledge_content

    def test_knowledge_has_arguments_variable(self, knowledge_content):
        """Knowledge skill has $ARGUMENTS for user input."""
        assert "$ARGUMENTS" in knowledge_content


class TestMemorySkillContent:
    @pytest.fixture
    def memory_content(self):
        definitions = get_meho_skill_definitions()
        return next(content for name, content in definitions if name == "meho-memory")

    def test_memory_name(self, memory_content):
        assert "name: meho:memory" in memory_content

    def test_memory_has_store_command(self, memory_content):
        """Memory skill includes store command."""
        assert "meho memory store" in memory_content

    def test_memory_has_search_command(self, memory_content):
        """Memory skill includes search command."""
        assert "meho memory search" in memory_content

    def test_memory_has_list_command(self, memory_content):
        """Memory skill includes list command."""
        assert "meho memory list" in memory_content

    def test_memory_has_forget_command(self, memory_content):
        """Memory skill includes forget command."""
        assert "meho memory forget" in memory_content

    def test_memory_has_arguments_variable(self, memory_content):
        """Memory skill has $ARGUMENTS for user input."""
        assert "$ARGUMENTS" in memory_content


# ---- write_meho_skills Tests ----


class TestWriteMehoSkills:
    def test_creates_all_five_directories_and_files(self, tmp_path):
        """write_meho_skills creates 5 directories with SKILL.md files."""
        results = write_meho_skills(tmp_path)
        assert len(results) == 5

        for result in results:
            skill_dir = tmp_path / result["name"]
            skill_file = skill_dir / "SKILL.md"
            assert skill_dir.exists()
            assert skill_file.exists()
            assert result["status"] == "created"
            assert result["path"] == str(skill_file)

    def test_returns_list_of_dicts_with_expected_keys(self, tmp_path):
        """Each result dict has name, status, and path keys."""
        results = write_meho_skills(tmp_path)
        for result in results:
            assert "name" in result
            assert "status" in result
            assert "path" in result

    def test_skips_existing_files_without_force(self, tmp_path):
        """Existing SKILL.md files are skipped when force=False."""
        # Create one existing skill
        existing_dir = tmp_path / "meho-diagnose"
        existing_dir.mkdir(parents=True)
        existing_file = existing_dir / "SKILL.md"
        existing_file.write_text("existing content")

        results = write_meho_skills(tmp_path, force=False)

        # Find the diagnose result
        diagnose_result = next(r for r in results if r["name"] == "meho-diagnose")
        assert diagnose_result["status"] == "skipped"

        # File content should be unchanged
        assert existing_file.read_text() == "existing content"

        # Other 4 should be created
        created = [r for r in results if r["status"] == "created"]
        assert len(created) == 4

    def test_overwrites_existing_files_with_force(self, tmp_path):
        """Existing SKILL.md files are overwritten when force=True."""
        # Create one existing skill
        existing_dir = tmp_path / "meho-diagnose"
        existing_dir.mkdir(parents=True)
        existing_file = existing_dir / "SKILL.md"
        existing_file.write_text("old content")

        results = write_meho_skills(tmp_path, force=True)

        # Find the diagnose result
        diagnose_result = next(r for r in results if r["name"] == "meho-diagnose")
        assert diagnose_result["status"] == "overwritten"

        # File content should be new
        assert existing_file.read_text() != "old content"

    def test_creates_parent_directories(self, tmp_path):
        """write_meho_skills creates parent dirs if they don't exist."""
        nested = tmp_path / "deep" / "nested" / "skills"
        results = write_meho_skills(nested)
        assert len(results) == 5
        assert nested.exists()

    def test_skill_file_content_is_valid(self, tmp_path):
        """Written SKILL.md files have valid YAML frontmatter."""
        write_meho_skills(tmp_path)

        for skill_dir in tmp_path.iterdir():
            skill_file = skill_dir / "SKILL.md"
            content = skill_file.read_text()
            assert content.startswith("---"), f"{skill_dir.name} SKILL.md missing frontmatter"
