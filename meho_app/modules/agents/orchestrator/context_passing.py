# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Context passing between specialists and orchestrator.

Phase 77: Replaces the raw findings injection with structured summaries
and replaces the UNRESOLVED regex parsing with structured entity extraction.

D-06: Structured entity output from specialists
D-12: Structured prior findings summary for specialists
D-13: Orchestrator builds summary from SubgraphOutputs
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger

if TYPE_CHECKING:
    from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput

logger = get_logger(__name__)

# Regex to extract <discovered_entities>...</discovered_entities> block
_DISCOVERED_ENTITIES_PATTERN = re.compile(
    r"<discovered_entities>\s*(.*?)\s*</discovered_entities>",
    re.DOTALL,
)


def parse_discovered_entities(findings: str) -> list[dict[str, Any]]:
    """Extract structured entities from specialist findings.

    Parses the <discovered_entities> JSON lines block emitted by specialists.
    Falls back gracefully: if no block found or parsing fails, returns empty list.

    Args:
        findings: Raw findings text from a specialist agent.

    Returns:
        List of entity dicts with keys: name, type, identifiers, connector_id, context.
    """
    if not findings:
        return []

    match = _DISCOVERED_ENTITIES_PATTERN.search(findings)
    if not match:
        return []

    entities: list[dict[str, Any]] = []
    block = match.group(1).strip()

    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entity = json.loads(line)
            # Validate minimum required fields
            if "name" in entity and "type" in entity:
                entities.append(entity)
            else:
                logger.debug(f"Skipping entity missing name/type: {line[:100]}")
        except json.JSONDecodeError:
            logger.debug(f"Failed to parse entity JSON: {line[:100]}")

    return entities


def build_structured_prior_findings(  # NOSONAR (cognitive complexity)
    findings: list[SubgraphOutput],
    max_chars_per_finding: int = 300,
) -> str:
    """Build compact structured summary for specialist context injection.

    Replaces raw findings text with actionable structured summary per D-12.
    Each connector's findings are summarized in 1-2 sentences.

    Args:
        findings: SubgraphOutput list from all previous specialists.
        max_chars_per_finding: Max characters per finding preview.

    Returns:
        Formatted prior findings context string, or empty string if no findings.
    """
    if not findings:
        return ""

    lines = ["## Prior Investigation Findings", ""]
    has_content = False

    for f in findings:
        if f.status not in ("success", "partial") or not f.findings:
            continue

        # Strip <discovered_entities> block from findings for summary
        clean_findings = _DISCOVERED_ENTITIES_PATTERN.sub("", f.findings).strip()
        if not clean_findings:
            continue

        # Truncate to max_chars_per_finding, break at sentence boundary
        if len(clean_findings) > max_chars_per_finding:
            truncated = clean_findings[:max_chars_per_finding]
            # Try to break at last sentence
            last_period = truncated.rfind(".")
            if last_period > max_chars_per_finding // 2:
                truncated = truncated[: last_period + 1]
            else:
                truncated = truncated.rstrip() + "..."
            preview = truncated
        else:
            preview = clean_findings

        status_marker = "OK" if f.status == "success" else "PARTIAL"
        lines.append(f"- **{f.connector_name}** [{status_marker}]: {preview}")
        has_content = True

    if not has_content:
        return ""

    lines.append("")
    lines.append(
        "Use these findings to guide your investigation. "
        "Do NOT re-investigate what was already found unless you have a specific reason."
    )
    return "\n".join(lines)


def build_routing_findings_context(
    findings: list[SubgraphOutput],
    max_chars_per_finding: int = 500,
) -> str:
    """Build findings context for the routing prompt.

    More detailed than the specialist summary -- the routing LLM needs
    enough detail to decide whether to query more or synthesize.

    Args:
        findings: SubgraphOutput list from all previous specialists.
        max_chars_per_finding: Max characters per finding.

    Returns:
        Formatted findings context for routing, or "None yet" if empty.
    """
    if not findings:
        return "None yet - this is the first iteration."

    lines = []
    for f in findings:
        status_icon = (
            "OK" if f.status == "success" else ("PARTIAL" if f.status == "partial" else "FAIL")
        )
        clean = _DISCOVERED_ENTITIES_PATTERN.sub("", f.findings).strip() if f.findings else ""

        if len(clean) > max_chars_per_finding:
            clean = clean[:max_chars_per_finding].rstrip() + "..."

        entry = f"[{status_icon} {f.connector_name}]: {clean}"
        if f.error_message:
            entry += f" (Error: {f.error_message})"
        lines.append(entry)

    return "\n\n".join(lines)
