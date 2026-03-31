"""Tests for skill markdown generation."""

from pathlib import Path

from meho_claude.core.skills import (
    generate_skill_markdown,
    write_skill_file,
    _diagnose_skill,
    _connect_skill,
    _topology_skill,
    get_meho_skill_definitions,
)


class TestGenerateSkillMarkdown:
    def test_returns_string(self):
        result = generate_skill_markdown(
            connector_name="petstore",
            connector_type="rest",
            description="A pet store API",
            operation_count=5,
            tag_groups={"pets": 4, "store": 1},
            auth_method="bearer",
            base_url="https://petstore.example.com",
        )
        assert isinstance(result, str)

    def test_contains_connector_name(self):
        result = generate_skill_markdown(
            connector_name="petstore",
            connector_type="rest",
            description="A pet store API",
            operation_count=5,
            tag_groups={"pets": 4, "store": 1},
            auth_method="bearer",
            base_url="https://petstore.example.com",
        )
        assert "petstore" in result

    def test_contains_operation_count(self):
        result = generate_skill_markdown(
            connector_name="petstore",
            connector_type="rest",
            description="A pet store API",
            operation_count=5,
            tag_groups={"pets": 4, "store": 1},
            auth_method="bearer",
            base_url="https://petstore.example.com",
        )
        assert "5" in result

    def test_contains_tag_groups(self):
        result = generate_skill_markdown(
            connector_name="petstore",
            connector_type="rest",
            description="A pet store API",
            operation_count=5,
            tag_groups={"pets": 4, "store": 1},
            auth_method="bearer",
            base_url="https://petstore.example.com",
        )
        assert "pets" in result
        assert "store" in result

    def test_contains_auth_method(self):
        result = generate_skill_markdown(
            connector_name="petstore",
            connector_type="rest",
            description="A pet store API",
            operation_count=5,
            tag_groups={"pets": 4, "store": 1},
            auth_method="bearer",
            base_url="https://petstore.example.com",
        )
        assert "bearer" in result.lower()

    def test_contains_quick_reference_section(self):
        result = generate_skill_markdown(
            connector_name="petstore",
            connector_type="rest",
            description="A pet store API",
            operation_count=5,
            tag_groups={"pets": 4, "store": 1},
            auth_method="bearer",
            base_url="https://petstore.example.com",
        )
        assert "Quick Reference" in result

    def test_contains_how_to_use_section(self):
        result = generate_skill_markdown(
            connector_name="petstore",
            connector_type="rest",
            description="A pet store API",
            operation_count=5,
            tag_groups={"pets": 4, "store": 1},
            auth_method="bearer",
            base_url="https://petstore.example.com",
        )
        assert "How to Use" in result

    def test_contains_trust_model_section(self):
        result = generate_skill_markdown(
            connector_name="petstore",
            connector_type="rest",
            description="A pet store API",
            operation_count=5,
            tag_groups={"pets": 4, "store": 1},
            auth_method="bearer",
            base_url="https://petstore.example.com",
        )
        assert "Trust Model" in result

    def test_contains_search_instruction(self):
        result = generate_skill_markdown(
            connector_name="petstore",
            connector_type="rest",
            description="A pet store API",
            operation_count=5,
            tag_groups={"pets": 4, "store": 1},
            auth_method="bearer",
            base_url="https://petstore.example.com",
        )
        assert "search-ops" in result

    def test_contains_call_instruction(self):
        result = generate_skill_markdown(
            connector_name="petstore",
            connector_type="rest",
            description="A pet store API",
            operation_count=5,
            tag_groups={"pets": 4, "store": 1},
            auth_method="bearer",
            base_url="https://petstore.example.com",
        )
        assert "call" in result


class TestWriteSkillFile:
    def test_writes_file_at_correct_path(self, tmp_path: Path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        content = "# Test Skill\nSome content"

        result_path = write_skill_file(skills_dir, "petstore", content)

        assert result_path == skills_dir / "petstore.md"
        assert result_path.exists()
        assert result_path.read_text() == content

    def test_overwrites_existing(self, tmp_path: Path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "petstore.md").write_text("old content")

        write_skill_file(skills_dir, "petstore", "new content")

        assert (skills_dir / "petstore.md").read_text() == "new content"

    def test_returns_path(self, tmp_path: Path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        result = write_skill_file(skills_dir, "test-api", "content")

        assert isinstance(result, Path)
        assert result.name == "test-api.md"


class TestDiagnoseSkill:
    def test_contains_all_connector_types(self):
        content = _diagnose_skill()
        assert "Proxmox" in content
        assert "GCP" in content

    def test_contains_visual_input_section(self):
        content = _diagnose_skill()
        assert "Visual Input" in content

    def test_contains_screenshot_prompting(self):
        content = _diagnose_skill()
        assert "screenshot" in content

    def test_starts_with_yaml_frontmatter(self):
        content = _diagnose_skill()
        assert content.startswith("---")


class TestConnectSkill:
    def test_contains_all_six_connector_types(self):
        content = _connect_skill()
        for connector_type in ["rest", "soap", "kubernetes", "vmware", "proxmox", "gcp"]:
            assert connector_type in content, f"Missing connector type: {connector_type}"

    def test_starts_with_yaml_frontmatter(self):
        content = _connect_skill()
        assert content.startswith("---")


class TestTopologySkill:
    def test_contains_correlate_command(self):
        content = _topology_skill()
        assert "correlate" in content

    def test_starts_with_yaml_frontmatter(self):
        content = _topology_skill()
        assert content.startswith("---")


class TestGetMehoSkillDefinitions:
    def test_returns_five_tuples(self):
        definitions = get_meho_skill_definitions()
        assert len(definitions) == 5

    def test_all_start_with_yaml_frontmatter(self):
        definitions = get_meho_skill_definitions()
        for dir_name, content in definitions:
            assert content.startswith("---"), f"{dir_name} missing YAML frontmatter"

    def test_all_tuples_have_dir_name_and_content(self):
        definitions = get_meho_skill_definitions()
        for item in definitions:
            assert isinstance(item, tuple)
            assert len(item) == 2
            assert isinstance(item[0], str)
            assert isinstance(item[1], str)
