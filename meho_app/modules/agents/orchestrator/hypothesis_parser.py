# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Hypothesis extraction from specialist agent thought events (Phase 62).

Parses XML hypothesis tags from agent thought content and returns
structured hypothesis updates for SSE emission.
"""

from __future__ import annotations

import re
from typing import Any

# Matches <hypothesis id="h-1" status="investigating">text</hypothesis>
HYPOTHESIS_PATTERN = re.compile(
    r'<hypothesis\s+id="([^"]+)"\s+status="([^"]+)">\s*(.*?)\s*</hypothesis>',
    re.DOTALL,
)


def extract_hypotheses(thought_content: str) -> list[dict[str, Any]]:
    """Extract hypothesis updates from agent thought text.

    Args:
        thought_content: Raw thought text that may contain hypothesis XML tags.

    Returns:
        List of hypothesis dicts with id, status, and text fields.
        Empty list if no hypothesis tags found.
    """
    matches = HYPOTHESIS_PATTERN.findall(thought_content)
    return [{"hypothesis_id": m[0], "status": m[1], "text": m[2].strip()} for m in matches]
