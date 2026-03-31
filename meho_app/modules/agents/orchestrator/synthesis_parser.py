# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Structured synthesis output parser (Phase 62).

Parses the XML-structured synthesis output into sections (summary, reasoning,
hypotheses, follow-ups) and builds the citation map linking [src:step-N] markers
to connector data_refs.
"""

from __future__ import annotations

import re
from typing import Any

from meho_app.core.otel import get_logger

logger = get_logger(__name__)

# Section extraction patterns
SUMMARY_PATTERN = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)
REASONING_PATTERN = re.compile(r"<reasoning>(.*?)</reasoning>", re.DOTALL)
HYPOTHESES_PATTERN = re.compile(r"<hypotheses>(.*?)</hypotheses>", re.DOTALL)
FOLLOW_UPS_PATTERN = re.compile(r"<follow_ups>(.*?)</follow_ups>", re.DOTALL)

# Individual item patterns
HYPOTHESIS_ITEM = re.compile(r'<hypothesis\s+status="([^"]+)">(.*?)</hypothesis>', re.DOTALL)
QUESTION_ITEM = re.compile(r"<question>(.*?)</question>", re.DOTALL)
CITATION_MARKER = re.compile(r"\[src:step-(\d+)\]")
CONNECTOR_SEGMENT = re.compile(r"\[connector:([^\]]+)\]")


def parse_synthesis_sections(text: str) -> dict[str, Any] | None:
    """Parse structured XML sections from synthesis output.

    Args:
        text: Full accumulated synthesis text.

    Returns:
        Dict with summary, reasoning, hypotheses, follow_ups, and
        connector_segments keys. Returns None if no <summary> tag found
        (indicating non-structured output -- fallback to flat rendering).
    """
    summary_match = SUMMARY_PATTERN.search(text)
    if not summary_match:
        return None  # Not structured output

    reasoning_match = REASONING_PATTERN.search(text)
    hypotheses_match = HYPOTHESES_PATTERN.search(text)
    follow_ups_match = FOLLOW_UPS_PATTERN.search(text)

    # Parse hypotheses
    hypotheses: list[dict[str, str]] = []
    if hypotheses_match:
        for m in HYPOTHESIS_ITEM.finditer(hypotheses_match.group(1)):
            hypotheses.append({"status": m.group(1), "text": m.group(2).strip()})

    # Parse follow-up questions
    follow_ups: list[str] = []
    if follow_ups_match:
        for m in QUESTION_ITEM.finditer(follow_ups_match.group(1)):
            follow_ups.append(m.group(1).strip())

    # Parse connector segments from reasoning
    reasoning_text = reasoning_match.group(1).strip() if reasoning_match else ""
    connector_segments: list[dict[str, str]] = []
    if reasoning_text:
        parts = CONNECTOR_SEGMENT.split(reasoning_text)
        # parts alternates: [text_before, connector_name, content, connector_name, content, ...]
        # Skip the first element (text before first [connector:])
        i = 1
        while i < len(parts) - 1:
            connector_name = parts[i].strip()
            content = parts[i + 1].strip() if i + 1 < len(parts) else ""
            connector_segments.append(
                {
                    "connector_name": connector_name,
                    "content": content,
                }
            )
            i += 2

    return {
        "summary": summary_match.group(1).strip(),
        "reasoning": reasoning_text,
        "hypotheses": hypotheses,
        "follow_ups": follow_ups,
        "connector_segments": connector_segments,
    }


def extract_follow_ups(text: str) -> list[str]:
    """Extract follow-up questions from synthesis text.

    Convenience function for extracting just the follow-ups.

    Args:
        text: Full accumulated synthesis text.

    Returns:
        List of follow-up question strings. Empty if none found.
    """
    match = FOLLOW_UPS_PATTERN.search(text)
    if not match:
        return []
    return [m.group(1).strip() for m in QUESTION_ITEM.finditer(match.group(1))]


def build_citation_map(
    text: str,
    findings: list[Any],
) -> dict[str, dict[str, Any]]:
    """Build citation number -> data_ref mapping from synthesis text and findings.

    Maps [src:step-N] markers in the synthesis text to the corresponding
    connector findings and their data_refs. Step numbers are 1-indexed and
    correspond to the sequential order of tool calls across all connector agents.

    Args:
        text: Full synthesis text containing [src:step-N] markers.
        findings: List of ConnectorFinding objects from orchestrator state
                  (each has connector_id, connector_name, connector_type,
                  data_refs, and status).

    Returns:
        Dict mapping citation number (string "1", "2", ...) to citation data:
        {
            "step_id": "step-3",
            "connector_id": "uuid",
            "connector_name": "Production K8s",
            "connector_type": "kubernetes",
            "data_ref": {"table": "...", "session_id": "...", "row_count": N}
        }
    """
    # Find all citation markers
    markers = CITATION_MARKER.findall(text)
    if not markers:
        return {}

    # Build a flat list of data_refs ordered by connector findings
    # Each finding may have multiple data_refs (one per tool call)
    ordered_refs: list[dict[str, Any]] = []
    for finding in findings:
        if finding.status != "success":
            continue
        data_refs = getattr(finding, "data_refs", None) or []
        for ref in data_refs:
            ordered_refs.append(
                {
                    "connector_id": finding.connector_id,
                    "connector_name": finding.connector_name,
                    "connector_type": getattr(finding, "connector_type", "rest"),
                    "data_ref": ref if isinstance(ref, dict) else {"table": str(ref)},
                }
            )

    citation_map: dict[str, dict[str, Any]] = {}
    seen_steps: set[str] = set()
    citation_num = 1

    for step_num_str in markers:
        step_key = f"step-{step_num_str}"
        if step_key in seen_steps:
            continue
        seen_steps.add(step_key)

        step_idx = int(step_num_str) - 1  # Convert to 0-indexed
        if 0 <= step_idx < len(ordered_refs):
            ref_data = ordered_refs[step_idx]
            citation_map[str(citation_num)] = {
                "step_id": step_key,
                **ref_data,
            }
        else:
            # Step number out of range -- create informational citation
            # Try to map to the closest connector by context
            logger.debug(
                f"Citation step-{step_num_str} out of range (have {len(ordered_refs)} data_refs)"
            )
            if ordered_refs:
                # Fall back to last known data_ref
                citation_map[str(citation_num)] = {
                    "step_id": step_key,
                    **ordered_refs[-1],
                }

        citation_num += 1

    return citation_map
