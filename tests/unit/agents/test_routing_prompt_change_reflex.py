# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for Phase 78: Change correlation reflex in routing and synthesis prompts.

Tests verify:
- Routing prompt contains the change correlation reflex rule
- Synthesis prompt contains change timeline presentation guidance
- Routing prompt template variables are preserved after modification
- Build functions produce prompts containing the new rules
"""
from pathlib import Path

from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput
from meho_app.modules.agents.orchestrator.routing import build_routing_prompt
from meho_app.modules.agents.orchestrator.state import OrchestratorState
from meho_app.modules.agents.orchestrator.synthesis import build_synthesis_prompt


ROUTING_TEMPLATE_PATH = Path(__file__).resolve().parent.parent.parent.parent / (
    "meho_app/modules/agents/orchestrator/prompts/routing.md"
)

SYNTHESIS_TEMPLATE_PATH = Path(__file__).resolve().parent.parent.parent.parent / (
    "meho_app/modules/agents/orchestrator/prompts/synthesis.md"
)


class TestRoutingPromptChangeReflex:
    """Tests for change correlation reflex rule in routing prompt."""

    def test_routing_template_contains_change_reflex_rule(self):
        """CHNG-01: Routing prompt template has the change correlation reflex rule."""
        content = ROUTING_TEMPLATE_PATH.read_text()
        assert "Change correlation reflex:" in content

    def test_routing_template_mentions_argocd_and_github(self):
        """CHNG-01: Rule explicitly names ArgoCD and GitHub as change sources."""
        content = ROUTING_TEMPLATE_PATH.read_text()
        assert "ArgoCD" in content
        assert "GitHub" in content

    def test_routing_template_has_skip_conditions(self):
        """CHNG-01: Rule includes explicit skip conditions for non-investigation queries."""
        content = ROUTING_TEMPLATE_PATH.read_text()
        assert "NOT an investigation" in content

    def test_routing_template_preserves_existing_rules(self):
        """Regression: All pre-existing rules still present after modification."""
        content = ROUTING_TEMPLATE_PATH.read_text()
        assert "Multi-part queries:" in content
        assert "Budget awareness:" in content
        assert "Convergence:" in content
        assert "Investigation skills:" in content
        assert "Novelty assessment:" in content

    def test_routing_template_preserves_template_variables(self):
        """Regression: All template variables still present."""
        content = ROUTING_TEMPLATE_PATH.read_text()
        for var in ["{connectors}", "{query}", "{findings}", "{iteration}",
                    "{remaining_budget}", "{investigation_skills}", "{history}"]:
            assert var in content, f"Missing template variable: {var}"

    def test_build_routing_prompt_contains_change_reflex(self):
        """CHNG-01: build_routing_prompt output includes the change reflex rule."""
        state = OrchestratorState(
            user_goal="Why is payment-service alerting?",
            max_iterations=5,
        )
        prompt = build_routing_prompt(
            state=state,
            connectors=[{"name": "K8s", "id": "k8s-1", "connector_type": "kubernetes",
                         "routing_description": "Production Kubernetes cluster"}],
        )
        assert "Change correlation reflex" in prompt


class TestSynthesisPromptChangeGuidance:
    """Tests for change timeline presentation guidance in synthesis prompt."""

    def test_synthesis_template_contains_causal_plausibility(self):
        """CHNG-02: Synthesis prompt template has causal plausibility guidance."""
        content = SYNTHESIS_TEMPLATE_PATH.read_text()
        assert "causal plausibility" in content

    def test_synthesis_template_contains_classification_categories(self):
        """CHNG-02: Synthesis prompt mentions all three classification categories."""
        content = SYNTHESIS_TEMPLATE_PATH.read_text()
        assert "infrastructure change" in content
        assert "application change" in content
        assert "non-functional change" in content

    def test_synthesis_template_anti_flat_timeline(self):
        """CHNG-02: Synthesis prompt explicitly discourages flat chronological lists."""
        content = SYNTHESIS_TEMPLATE_PATH.read_text()
        assert "Do NOT present a flat chronological list" in content

    def test_synthesis_template_preserves_response_format(self):
        """Regression: Response format XML structure unchanged."""
        content = SYNTHESIS_TEMPLATE_PATH.read_text()
        assert "<summary>" in content
        assert "<reasoning>" in content
        assert "<hypotheses>" in content
        assert "<follow_ups>" in content

    def test_synthesis_template_preserves_template_variables(self):
        """Regression: All template variables still present."""
        content = SYNTHESIS_TEMPLATE_PATH.read_text()
        for var in ["{query}", "{findings}", "{history}", "{budget_context}"]:
            assert var in content, f"Missing template variable: {var}"

    def test_build_synthesis_prompt_contains_change_guidance(self):
        """CHNG-02: build_synthesis_prompt output includes causal plausibility guidance."""
        state = OrchestratorState(
            user_goal="Why is payment-service down?",
            max_iterations=5,
        )
        # Add a finding so get_findings_summary returns something
        state.add_iteration_findings([
            SubgraphOutput(
                connector_id="argocd-1",
                connector_name="ArgoCD Prod",
                findings="payment-service synced at 14:36",
            )
        ])
        prompt = build_synthesis_prompt(state=state)
        assert "causal plausibility" in prompt
