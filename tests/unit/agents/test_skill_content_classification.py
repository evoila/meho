# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for Phase 78: Causal plausibility classification in skill content.

Tests verify:
- CHNG-02: Both change skills include classification taxonomy (infrastructure/application/non-functional)
- CHNG-03: Both change skills include entity-scoped query guidance and 2-hour time window
- Regression: Skill names, descriptions, summaries unchanged
- Regression: Other skills not accidentally modified
"""

from meho_app.modules.orchestrator_skills.seed import (
    CHANGE_CORRELATION_SKILL_CONTENT,
    CHANGE_CORRELATION_SKILL_DESCRIPTION,
    CHANGE_CORRELATION_SKILL_NAME,
    CHANGE_CORRELATION_SKILL_SUMMARY,
    INCIDENT_CHANGE_SKILL_CONTENT,
    INCIDENT_CHANGE_SKILL_DESCRIPTION,
    INCIDENT_CHANGE_SKILL_NAME,
    INCIDENT_CHANGE_SKILL_SUMMARY,
    INFRASTRUCTURE_PERF_SKILL_CONTENT,
    LOG_DRIVEN_SKILL_CONTENT,
    SERVICE_DEPENDENCY_SKILL_CONTENT,
)


class TestChangeCorrelationClassification:
    """CHNG-02: Change Correlation skill includes classification taxonomy."""

    def test_has_classification_section(self):
        """Skill content has a causal plausibility classification section."""
        assert (
            "Causal Plausibility" in CHANGE_CORRELATION_SKILL_CONTENT
            or "Classify by Causal Plausibility" in CHANGE_CORRELATION_SKILL_CONTENT
        )

    def test_has_application_change_category(self):
        """Skill classifies application changes (ArgoCD sync, GitHub merge, etc.)."""
        assert "Application Change" in CHANGE_CORRELATION_SKILL_CONTENT

    def test_has_infrastructure_change_category(self):
        """Skill classifies infrastructure changes (node cordon, VM migration, etc.)."""
        assert "Infrastructure Change" in CHANGE_CORRELATION_SKILL_CONTENT

    def test_has_non_functional_change_category(self):
        """Skill classifies non-functional changes (docs, CI config, tests)."""
        assert "Non-Functional Change" in CHANGE_CORRELATION_SKILL_CONTENT

    def test_classification_prioritizes_by_plausibility(self):
        """Skill instructs to present by plausibility, not chronologically."""
        assert "plausibility" in CHANGE_CORRELATION_SKILL_CONTENT.lower()
        assert (
            "not chronologically" in CHANGE_CORRELATION_SKILL_CONTENT.lower()
            or "grouped by" in CHANGE_CORRELATION_SKILL_CONTENT.lower()
        )


class TestIncidentChangeClassification:
    """CHNG-02: Incident-to-Change skill includes classification taxonomy."""

    def test_has_classification_section(self):
        """Skill content has a classification section."""
        assert "Classify" in INCIDENT_CHANGE_SKILL_CONTENT

    def test_has_application_change_category(self):
        """Skill classifies application changes."""
        assert "Application Change" in INCIDENT_CHANGE_SKILL_CONTENT

    def test_has_infrastructure_change_category(self):
        """Skill classifies infrastructure changes."""
        assert "Infrastructure Change" in INCIDENT_CHANGE_SKILL_CONTENT

    def test_has_non_functional_change_category(self):
        """Skill classifies non-functional changes."""
        assert "Non-Functional Change" in INCIDENT_CHANGE_SKILL_CONTENT


class TestChangeCorrelationEntityScoping:
    """CHNG-03: Change Correlation skill includes entity-scoped query guidance."""

    def test_has_entity_scoping_section(self):
        """Skill has an entity-scoped change queries section."""
        assert "Entity-Scoped Change Queries" in CHANGE_CORRELATION_SKILL_CONTENT

    def test_references_prior_findings(self):
        """Skill tells agent to use prior specialist findings for scoping."""
        assert "prior findings" in CHANGE_CORRELATION_SKILL_CONTENT.lower()

    def test_references_two_hour_window(self):
        """Skill reinforces the 2-hour default lookback window."""
        assert "2 hour" in CHANGE_CORRELATION_SKILL_CONTENT.lower()

    def test_noise_filtering_guidance(self):
        """Skill tells agent to avoid listing unrelated changes."""
        lower = CHANGE_CORRELATION_SKILL_CONTENT.lower()
        assert "noise" in lower or "unrelated" in lower


class TestIncidentChangeEntityScoping:
    """CHNG-03: Incident-to-Change skill includes entity-scoped query guidance."""

    def test_has_entity_scoping_section(self):
        """Skill has an entity-scoped change queries section."""
        assert "Entity-Scoped Change Queries" in INCIDENT_CHANGE_SKILL_CONTENT

    def test_references_topology_neighbors(self):
        """Skill tells agent to check topology neighbors for infrastructure context."""
        lower = INCIDENT_CHANGE_SKILL_CONTENT.lower()
        assert "topology" in lower

    def test_references_time_window(self):
        """Skill reinforces time window scoping."""
        assert "2 hour" in INCIDENT_CHANGE_SKILL_CONTENT.lower()

    def test_noise_filtering_guidance(self):
        """Skill tells agent to filter irrelevant changes."""
        lower = INCIDENT_CHANGE_SKILL_CONTENT.lower()
        assert "noise" in lower or "irrelevant" in lower


class TestSkillMetadataUnchanged:
    """Regression: Skill names, descriptions, and summaries must not change."""

    def test_change_correlation_name_unchanged(self):
        assert CHANGE_CORRELATION_SKILL_NAME == "Change Correlation"

    def test_change_correlation_summary_starts_correctly(self):
        assert CHANGE_CORRELATION_SKILL_SUMMARY.startswith(
            "When an operator asks"
        )

    def test_incident_change_name_unchanged(self):
        assert INCIDENT_CHANGE_SKILL_NAME == "Incident-to-Change Correlation"

    def test_incident_change_summary_starts_correctly(self):
        assert INCIDENT_CHANGE_SKILL_SUMMARY.startswith(
            "When investigating incidents"
        )


class TestOtherSkillsUntouched:
    """Regression: Other investigation skills must not be accidentally modified."""

    def test_infrastructure_perf_no_classification(self):
        """Infrastructure Performance Cascade should not have classification taxonomy."""
        assert "Causal Plausibility Classification" not in INFRASTRUCTURE_PERF_SKILL_CONTENT
        assert "Entity-Scoped Change Queries" not in INFRASTRUCTURE_PERF_SKILL_CONTENT

    def test_service_dependency_no_classification(self):
        """Service Dependency Failure should not have classification taxonomy."""
        assert "Causal Plausibility Classification" not in SERVICE_DEPENDENCY_SKILL_CONTENT
        assert "Entity-Scoped Change Queries" not in SERVICE_DEPENDENCY_SKILL_CONTENT

    def test_log_driven_no_classification(self):
        """Log-Driven Error Investigation should not have classification taxonomy."""
        assert "Causal Plausibility Classification" not in LOG_DRIVEN_SKILL_CONTENT
        assert "Entity-Scoped Change Queries" not in LOG_DRIVEN_SKILL_CONTENT
