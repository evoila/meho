# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""SpecialistAgent - Unified connector-scoped agent parameterized by skill.

This module provides the SpecialistAgent class, the primary connector-scoped
agent. A single SpecialistAgent is parameterized by injectable markdown skills
that become part of the system prompt.

Usage:
    from meho_app.modules.agents.specialist_agent import SpecialistAgent

    agent = SpecialistAgent(
        dependencies=deps,
        connector_id="k8s-prod-123",
        connector_name="Production K8s",
        connector_type="kubernetes",
        skill_content=loaded_skill_markdown,
    )
    async for event in agent.run_streaming(user_message):
        yield event
"""

from meho_app.modules.agents.specialist_agent.agent import SpecialistAgent

__all__ = ["SpecialistAgent"]
