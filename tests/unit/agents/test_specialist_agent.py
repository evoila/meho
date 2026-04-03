# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for SpecialistAgent.

Tests for:
- SpecialistAgent class structure (fields, inheritance)
- Prompt template structure (placeholders, skill slot, tools)
- No K8s-specific content in the template itself
"""

from pathlib import Path

import pytest

from meho_app.modules.agents.specialist_agent.agent import SpecialistAgent

# =============================================================================
# Class Structure Tests
# =============================================================================


class TestSpecialistAgentStructure:
    """Tests for SpecialistAgent class definition and fields."""

    def test_specialist_agent_has_correct_agent_name(self):
        """Test that SpecialistAgent has agent_name = 'specialist'."""
        assert SpecialistAgent.agent_name == "specialist"

    def test_specialist_agent_has_skill_content_field(self):
        """Test that skill_content is a dataclass field with default ''."""
        import dataclasses

        fields = {f.name: f for f in dataclasses.fields(SpecialistAgent)}
        assert "skill_content" in fields, "skill_content field missing"
        assert fields["skill_content"].default == ""

    def test_specialist_agent_has_connector_fields(self):
        """Test that connector_id, connector_name, connector_type, routing_description exist."""
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(SpecialistAgent)}
        expected = {
            "connector_id",
            "connector_name",
            "connector_type",
            "routing_description",
        }
        assert expected.issubset(field_names), f"Missing fields: {expected - field_names}"

    def test_specialist_agent_extends_base_agent(self):
        """Test that SpecialistAgent is a subclass of BaseAgent."""
        from meho_app.modules.agents.base.agent import BaseAgent

        assert issubclass(SpecialistAgent, BaseAgent)


# =============================================================================
# Prompt Template Tests
# =============================================================================


class TestSpecialistAgentPromptTemplate:
    """Tests for the specialist agent's system prompt template."""

    @pytest.fixture
    def template_content(self) -> str:
        """Load the system prompt template."""
        template_path = (
            Path(__file__).resolve().parents[3]
            / "meho_app"
            / "modules"
            / "agents"
            / "specialist_agent"
            / "prompts"
            / "system.md"
        )
        assert template_path.exists(), f"Template not found: {template_path}"
        return template_path.read_text()

    def test_specialist_agent_prompt_template_has_skill_slot(self, template_content: str):
        """Test that the prompt template contains {{skill_content}} placeholder."""
        assert "{{skill_content}}" in template_content

    def test_specialist_agent_prompt_template_has_connector_placeholders(
        self, template_content: str
    ):
        """Test that the template has connector-scoped placeholders."""
        for placeholder in [
            "{{connector_id}}",
            "{{connector_name}}",
            "{{connector_type}}",
            "{{routing_description}}",
        ]:
            assert placeholder in template_content, f"Missing placeholder: {placeholder}"

    def test_specialist_agent_prompt_template_has_tool_section(self, template_content: str):
        """Test that the template has <tools> section with key tools."""
        assert "<tools>" in template_content
        assert "search_operations" in template_content
        assert "call_operation" in template_content
        assert "reduce_data" in template_content

    def test_specialist_agent_prompt_template_no_k8s_specific_content(self, template_content: str):
        """Test that the TEMPLATE does not contain K8s-specific knowledge.

        K8s knowledge belongs in the skill file (kubernetes.md), not in
        the generic template shared by all connector types.
        """
        # The template should not hard-code K8s resource types
        k8s_specific_terms = ["kubectl", "kubelet", "kube-proxy", "etcd"]
        for term in k8s_specific_terms:
            assert term not in template_content.lower(), (
                f"K8s-specific term '{term}' found in template -- "
                f"belongs in skills/kubernetes.md instead"
            )
