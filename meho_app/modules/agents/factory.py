# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Agent factory for creating the appropriate agent per connector.

Resolves the correct skill file for a connector and constructs a
SpecialistAgent with the resolved skill content.

Skill resolution (two-phase):
    Phase 1 -- Base skill:
        0. DB-stored connector skill from unified skill system (Phase 77)
        1. DB-stored generated_skill from connector record (pipeline-owned)
        2. Explicit ``skill_name`` on connector (e.g., "custom_crm.md")
        3. Type-level default (e.g., "kubernetes" -> "kubernetes.md")
        4. Generic fallback ("generic.md")

    Phase 2 -- Instance append:
        custom_skill (operator-owned) is ALWAYS appended to whatever base
        skill was resolved. Type-level skill is the base; instance custom_skill
        appends additional context (does NOT replace).

Usage:
    from meho_app.modules.agents.factory import create_agent

    agent = create_agent(
        dependencies=deps,
        connector_id="abc-123",
        connector_name="Production K8s",
        connector_type="kubernetes",
        routing_description="Production Kubernetes cluster",
    )
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from meho_app.core.otel.logging import get_logger

logger = get_logger(__name__)

SKILLS_DIR = Path(__file__).parent / "skills"

# Type-level skill defaults -- typed connectors get their type-specific skill
TYPE_SKILL_MAP: dict[str, str] = {
    "kubernetes": "kubernetes.md",
    "vmware": "vmware.md",
    "proxmox": "proxmox.md",
    "gcp": "gcp.md",
    "prometheus": "prometheus.md",
    "loki": "loki.md",
    "tempo": "tempo.md",
    "alertmanager": "alertmanager.md",
    "jira": "jira.md",
    "confluence": "confluence.md",
    "email": "email.md",
    "argocd": "argocd.md",
    "github": "github.md",
    "azure": "azure.md",
    "aws": "aws.md",
    "mcp": "mcp.md",
    "slack": "slack.md",
}


def _append_instance_skill(
    base_skill: str,
    custom_skill: str | None,
) -> str:
    """Append instance-level custom_skill to the base skill.

    Type-level skill is the base. Instance custom_skill appends additional
    context per user decision -- it does NOT replace the type skill.

    Args:
        base_skill: The resolved base skill content (generated or filesystem).
        custom_skill: Operator-customized skill content from connector DB, or None.

    Returns:
        Combined skill content with instance context appended, or base_skill as-is.
    """
    if not custom_skill:
        return base_skill

    # D-03: Dedup guard -- prevent doubled content when custom == generated
    if custom_skill.strip() == base_skill.strip():
        return base_skill

    return base_skill + "\n\n<!-- Instance-Specific Context -->\n\n" + custom_skill


def create_agent(
    dependencies: Any,
    connector_id: str,
    connector_name: str,
    connector_type: str,
    routing_description: str = "",
    skill_name: str | None = None,
    iteration: int = 1,
    prior_findings: list[str] | None = None,
    generated_skill: str | None = None,
    custom_skill: str | None = None,
    db_connector_skill: str | None = None,
) -> Any:
    """Create a SpecialistAgent for a connector.

    Skill resolution (two-phase):
        Phase 1 -- Base skill:
            0. DB-stored connector skill from unified skill system (Phase 77)
            1. DB-stored generated_skill (pipeline-owned)
            2. Explicit skill_name on connector (e.g., "custom_crm.md")
            3. Type-level default (e.g., "kubernetes" -> "kubernetes.md")
            4. Generic fallback ("generic.md")

        Phase 2 -- Instance append:
            custom_skill is ALWAYS appended to whatever base skill was resolved.

    Args:
        dependencies: Injected service container.
        connector_id: UUID of the connector.
        connector_name: Human-readable connector name.
        connector_type: Type of connector (kubernetes, vmware, etc.).
        routing_description: What this connector manages.
        skill_name: Explicit skill file name override.
        iteration: Which orchestrator iteration this is part of.
        prior_findings: Findings from previous iterations.
        generated_skill: DB-stored generated skill content (pipeline-owned).
        custom_skill: DB-stored custom skill content (operator-owned).
        db_connector_skill: DB-stored connector skill content from the unified
            skill system (Phase 77). Resolved by the orchestrator before calling
            create_agent, avoiding async in the factory.

    Returns:
        A SpecialistAgent instance.
    """
    findings = prior_findings if prior_findings is not None else []

    # ── Phase 1: Resolve base skill ──────────────────────────────────────
    # Priority 0: DB-stored connector skill (Phase 77: unified skill system)
    if db_connector_skill:
        base_skill = db_connector_skill
        logger.info(f"Factory: using DB connector skill for {connector_name}")
    # Priority 1: DB-stored generated_skill from pipeline
    elif generated_skill:
        base_skill = generated_skill
        logger.info(f"Factory: using DB generated_skill for {connector_name}")
    else:
        # Priority 2-4: Filesystem skills (explicit > type-default > generic)
        resolved_skill = _resolve_skill_name(connector_type, skill_name)
        base_skill = _load_skill_content(resolved_skill)
        logger.info(f"Factory: using filesystem skill '{resolved_skill}' for {connector_name}")

    # ── Phase 2: Append instance custom_skill ────────────────────────────
    # Type-level skill is the base. Instance custom_skill appends additional
    # context per user decision -- it does NOT replace the type skill.
    skill_content = _append_instance_skill(base_skill, custom_skill)
    if custom_skill:
        logger.info(f"Factory: appended instance custom_skill for {connector_name}")

    # ── Phase 96.1: Append diagnostic skill universally ──────────────────
    # Network diagnostic tools are not connector-specific -- the skill is
    # injected into ALL specialist agents when the feature flag is enabled.
    from meho_app.core.feature_flags import get_feature_flags

    _flags = get_feature_flags()
    if _flags.network_diagnostics:
        diagnostic_skill_path = SKILLS_DIR / "network_diagnostics.md"
        if diagnostic_skill_path.exists():
            diagnostic_skill = diagnostic_skill_path.read_text()
            skill_content += "\n\n<!-- Network Diagnostics (universal) -->\n\n" + diagnostic_skill
            logger.info(f"Factory: appended diagnostic skill for {connector_name}")

    from meho_app.modules.agents.specialist_agent.agent import SpecialistAgent

    return SpecialistAgent(
        dependencies=dependencies,
        connector_id=connector_id,
        connector_name=connector_name,
        connector_type=connector_type,
        routing_description=routing_description,
        skill_content=skill_content,
        iteration=iteration,
        prior_findings=findings,
    )


def _resolve_skill_name(connector_type: str, skill_name: str | None) -> str:
    """Resolve which skill file to use.

    Priority: explicit skill_name > type default > generic fallback.

    Args:
        connector_type: The connector type (kubernetes, vmware, etc.).
        skill_name: Explicit skill override from connector config.

    Returns:
        The skill file name to load (e.g., "kubernetes.md").
    """
    if skill_name:
        return skill_name
    return TYPE_SKILL_MAP.get(connector_type, "generic.md")


def _load_skill_content(skill_filename: str) -> str:
    """Load skill content from the skills directory.

    Args:
        skill_filename: Name of the skill file (e.g., "kubernetes.md").

    Returns:
        The skill file contents, or empty string if file not found.
    """
    skill_path = SKILLS_DIR / skill_filename
    if not skill_path.exists():
        logger.warning(f"Skill file not found: {skill_path} -- using empty skill")
        return ""
    return skill_path.read_text()
