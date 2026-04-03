# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Sanitization for operation descriptions before skill generation.

Strips prompt injection patterns from API descriptions without destroying
legitimate documentation content. Applied BEFORE the generation LLM call,
NOT at storage time (original descriptions are preserved in the DB).

Key design principle: Only match structurally suspicious patterns (XML tags,
explicit override commands), NOT common documentation phrases like "you must
provide" or "always returns". See RESEARCH.md Pitfall 2.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meho_app.modules.connectors.skill_generation.quality_scorer import OperationData


# Compiled regex patterns that indicate prompt injection in API descriptions.
# These target structurally suspicious content, not common English phrases.
INJECTION_PATTERNS: list[re.Pattern[str]] = [
    # XML-style injection blocks that attempt to override agent behavior
    re.compile(r"(?i)<(?:system|role|instructions|rules|task)>"),
    # Explicit instruction override commands (multi-word phrases only)
    re.compile(r"(?i)(?:ignore previous instructions|disregard all|forget everything above)"),
    # System prompt leak attempts (structural phrases, not common docs language)
    re.compile(r"(?i)(?:system prompt|your instructions are|you are now)"),
]


def sanitize_description(text: str | None) -> str:
    """Remove prompt injection patterns from a single description.

    Replaces structurally suspicious content with [FILTERED] while preserving
    legitimate API documentation text.

    Args:
        text: Raw description text, or None.

    Returns:
        Sanitized description string. Empty string for None input.
    """
    if not text:
        return ""

    sanitized = text
    for pattern in INJECTION_PATTERNS:
        sanitized = pattern.sub("[FILTERED]", sanitized)

    return sanitized.strip()


def sanitize_descriptions(
    operations: list[OperationData],
) -> list[OperationData]:
    """Sanitize description and summary fields across a list of operations.

    Creates new OperationData instances with sanitized text fields using
    Pydantic's model_copy(). Does not modify the original objects.

    Args:
        operations: List of operation data with potentially unsafe descriptions.

    Returns:
        New list of OperationData with sanitized description and summary fields.
    """

    return [
        op.model_copy(
            update={
                "description": sanitize_description(op.description),
                "summary": sanitize_description(op.summary),
            }
        )
        for op in operations
    ]
