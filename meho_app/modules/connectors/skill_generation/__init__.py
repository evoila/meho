# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Skill generation pipeline for connector operations.

Transforms raw connector operations into SRE-style diagnostic playbooks
using LLM synthesis. The generated skills teach the SpecialistAgent how
to investigate and diagnose systems using exact operation_ids.

Public API:
    SkillGenerator: Main service class with generate_skill() pipeline
    SkillGenerationResult: Result model with content, score, and count
    compute_quality_score: Metadata completeness scoring (1-5 stars)
    sanitize_descriptions: Prompt injection sanitization for descriptions
"""

from meho_app.modules.connectors.skill_generation.generator import (
    SkillGenerationResult,
    SkillGenerator,
)
from meho_app.modules.connectors.skill_generation.quality_scorer import (
    compute_quality_score,
)
from meho_app.modules.connectors.skill_generation.sanitizer import (
    sanitize_descriptions,
)

__all__ = [
    "SkillGenerationResult",
    "SkillGenerator",
    "compute_quality_score",
    "sanitize_descriptions",
]
