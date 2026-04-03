# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for the agent factory.

Tests for:
- TYPE_SKILL_MAP completeness
- SKILLS_DIR existence and skill file availability
- Skill resolution priority (explicit > type-default > generic)
- Factory always returns SpecialistAgent (single code path)
- Skill content is loaded into SpecialistAgent
- DB skill resolution (generated_skill + custom_skill priority)
"""

from unittest.mock import MagicMock

import pytest

from meho_app.modules.agents.factory import (
    SKILLS_DIR,
    TYPE_SKILL_MAP,
    _append_instance_skill,
    _load_skill_content,
    _resolve_skill_name,
    create_agent,
)

# =============================================================================
# Skill Map and Directory Tests
# =============================================================================


class TestFactorySkillMap:
    """Tests for TYPE_SKILL_MAP and SKILLS_DIR."""

    def test_factory_type_skill_map_has_all_typed_connectors(self):
        """Test that TYPE_SKILL_MAP covers all typed connectors."""
        expected_types = {
            "alertmanager",
            "argocd",
            "aws",
            "azure",
            "confluence",
            "email",
            "gcp",
            "github",
            "jira",
            "kubernetes",
            "loki",
            "mcp",
            "prometheus",
            "proxmox",
            "slack",
            "tempo",
            "vmware",
        }
        assert expected_types == set(TYPE_SKILL_MAP.keys())

    def test_factory_skills_dir_exists(self):
        """Test that SKILLS_DIR path exists on disk."""
        assert SKILLS_DIR.exists(), f"SKILLS_DIR does not exist: {SKILLS_DIR}"
        assert SKILLS_DIR.is_dir(), f"SKILLS_DIR is not a directory: {SKILLS_DIR}"

    def test_factory_all_mapped_skills_exist(self):
        """Test that every skill file in TYPE_SKILL_MAP exists."""
        for connector_type, skill_file in TYPE_SKILL_MAP.items():
            skill_path = SKILLS_DIR / skill_file
            assert skill_path.exists(), f"Skill file missing for {connector_type}: {skill_path}"

    def test_factory_generic_fallback_skill_exists(self):
        """Test that generic.md fallback skill exists."""
        generic_path = SKILLS_DIR / "generic.md"
        assert generic_path.exists(), f"generic.md missing: {generic_path}"


# =============================================================================
# Skill Resolution Tests
# =============================================================================


class TestSkillResolution:
    """Tests for skill name resolution logic."""

    def test_skill_resolution_explicit_name(self):
        """Test that explicit skill_name takes precedence over type default."""
        result = _resolve_skill_name("kubernetes", "custom_crm.md")
        assert result == "custom_crm.md"

    def test_skill_resolution_type_default(self):
        """Test that typed connector resolves to its type-specific skill."""
        result = _resolve_skill_name("kubernetes", None)
        assert result == "kubernetes.md"

    def test_skill_resolution_generic_fallback(self):
        """Test that REST connector without skill_name resolves to generic.md."""
        result = _resolve_skill_name("rest", None)
        assert result == "generic.md"

    def test_skill_resolution_unknown_type_falls_back_to_generic(self):
        """Test that an unknown connector type gets generic.md."""
        result = _resolve_skill_name("some_new_type", None)
        assert result == "generic.md"


# =============================================================================
# Factory Agent Creation Tests
# =============================================================================


class TestFactoryAgentCreation:
    """Tests for create_agent() factory function."""

    @pytest.fixture
    def mock_deps(self):
        """Create mock dependencies."""
        return MagicMock()

    def test_factory_returns_specialist_for_kubernetes(self, mock_deps):
        """Test that factory returns SpecialistAgent for kubernetes connector."""
        from meho_app.modules.agents.specialist_agent.agent import SpecialistAgent

        agent = create_agent(
            dependencies=mock_deps,
            connector_id="test-123",
            connector_name="Test K8s",
            connector_type="kubernetes",
            routing_description="Test cluster",
        )

        assert isinstance(agent, SpecialistAgent)
        assert agent.connector_id == "test-123"
        assert agent.connector_name == "Test K8s"
        assert agent.connector_type == "kubernetes"

    def test_factory_returns_specialist_for_rest(self, mock_deps):
        """Test that factory returns SpecialistAgent for REST connector."""
        from meho_app.modules.agents.specialist_agent.agent import SpecialistAgent

        agent = create_agent(
            dependencies=mock_deps,
            connector_id="rest-123",
            connector_name="REST API",
            connector_type="rest",
        )

        assert isinstance(agent, SpecialistAgent)

    def test_factory_specialist_has_loaded_skill_content(self, mock_deps):
        """Test that SpecialistAgent created for kubernetes has K8s skill content."""
        agent = create_agent(
            dependencies=mock_deps,
            connector_id="k8-456",
            connector_name="Prod K8s",
            connector_type="kubernetes",
        )

        # Skill content should be non-empty and contain K8s-related terms
        assert agent.skill_content, "skill_content should not be empty"
        content_lower = agent.skill_content.lower()
        assert "kubernetes" in content_lower or "k8s" in content_lower, (
            "Kubernetes skill should contain K8s-related knowledge"
        )


# =============================================================================
# Skill Content Loading Tests
# =============================================================================


class TestSkillContentLoading:
    """Tests for _load_skill_content helper."""

    def test_load_existing_skill(self):
        """Test loading a skill file that exists."""
        content = _load_skill_content("kubernetes.md")
        assert content, "kubernetes.md should have content"
        assert len(content) > 50, "kubernetes.md should have substantial content"

    def test_load_nonexistent_skill_returns_empty(self):
        """Test that loading a nonexistent skill returns empty string."""
        content = _load_skill_content("does_not_exist_xyz.md")
        assert content == ""


# =============================================================================
# DB Skill Resolution Tests
# =============================================================================


class TestFactoryDBSkillPriority:
    """Tests for DB skill priority in create_agent()."""

    @pytest.fixture
    def mock_deps(self):
        """Create mock dependencies."""
        return MagicMock()

    def test_create_agent_prefers_db_skill_over_filesystem(self, mock_deps):
        """When generated_skill is provided, factory should use DB skill
        instead of filesystem skill."""
        from meho_app.modules.agents.specialist_agent.agent import SpecialistAgent

        agent = create_agent(
            dependencies=mock_deps,
            connector_id="test-db-1",
            connector_name="DB Skill Connector",
            connector_type="kubernetes",
            routing_description="Test cluster",
            generated_skill="# DB Generated Skill\n\nThis skill was generated by the pipeline.",
        )

        assert isinstance(agent, SpecialistAgent)
        # Should use the DB skill, NOT the filesystem kubernetes.md
        assert "DB Generated Skill" in agent.skill_content
        assert "pipeline" in agent.skill_content

    def test_create_agent_concatenates_generated_and_custom(self, mock_deps):
        """When both generated and custom skills exist, factory should
        concatenate them with custom after generated."""
        from meho_app.modules.agents.specialist_agent.agent import SpecialistAgent

        agent = create_agent(
            dependencies=mock_deps,
            connector_id="test-db-2",
            connector_name="Both Skills Connector",
            connector_type="rest",
            generated_skill="# Generated Part",
            custom_skill="# Custom Operator Notes",
        )

        assert isinstance(agent, SpecialistAgent)
        assert "Generated Part" in agent.skill_content
        assert "Custom Operator Notes" in agent.skill_content
        assert "Instance-Specific Context" in agent.skill_content

    def test_create_agent_falls_back_to_filesystem_when_no_db_skill(self, mock_deps):
        """When no DB skills are provided, factory should fall back to
        filesystem skill resolution (existing behavior)."""
        from meho_app.modules.agents.specialist_agent.agent import SpecialistAgent

        agent = create_agent(
            dependencies=mock_deps,
            connector_id="test-fs-1",
            connector_name="Filesystem Connector",
            connector_type="kubernetes",
            generated_skill=None,
            custom_skill=None,
        )

        assert isinstance(agent, SpecialistAgent)
        # Should use the filesystem kubernetes.md skill
        content_lower = agent.skill_content.lower()
        assert "kubernetes" in content_lower or "k8s" in content_lower, (
            "Should fall back to filesystem kubernetes.md skill"
        )


# =============================================================================
# Skill Dedup Guard Tests (Phase 89.1)
# =============================================================================


class TestAppendInstanceSkillDedup:
    """Tests for dedup guard in _append_instance_skill().

    D-03: When custom_skill.strip() == base_skill.strip(), return base_skill
    without appending to prevent doubled content in the system prompt.
    """

    def test_append_instance_skill_dedup_exact_match(self):
        """Identical content returns base only -- no doubling."""
        base = "# Kubernetes Skill\n\nManage pods and deployments."
        custom = "# Kubernetes Skill\n\nManage pods and deployments."
        result = _append_instance_skill(base, custom)
        assert result == base
        assert "Instance-Specific Context" not in result

    def test_append_instance_skill_dedup_whitespace_variants(self):
        """Trailing newlines and leading spaces still dedup."""
        base = "# Skill Content\n\nSome instructions."
        custom = "  # Skill Content\n\nSome instructions.  \n\n"
        result = _append_instance_skill(base.strip(), custom)
        # After strip(), both should match
        assert "Instance-Specific Context" not in result

    def test_append_instance_skill_different_content_appends(self):
        """Different content concatenates with separator."""
        base = "# Generated Skill"
        custom = "# Custom Operator Notes"
        result = _append_instance_skill(base, custom)
        assert "Generated Skill" in result
        assert "Custom Operator Notes" in result
        assert "Instance-Specific Context" in result

    def test_append_instance_skill_none_returns_base(self):
        """None custom_skill returns base unchanged."""
        base = "# Base Skill"
        result = _append_instance_skill(base, None)
        assert result == base

    def test_append_instance_skill_empty_returns_base(self):
        """Empty string custom_skill returns base unchanged."""
        base = "# Base Skill"
        result = _append_instance_skill(base, "")
        assert result == base
