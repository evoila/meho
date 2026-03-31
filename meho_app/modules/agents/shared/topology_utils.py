# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Shared topology formatting utilities.

Pure functions for parsing verification evidence, extracting entity mentions,
and formatting key attributes from topology data. Used by specialist_agent,
react_agent, and graph-based topology lookup nodes.
"""

from __future__ import annotations

import contextlib
import json
import re


def parse_verification_evidence(verified_via: list[str]) -> tuple[str, str]:
    """Parse verified_via list into human-readable confidence and evidence.

    The verified_via format from DeterministicResolver:
    ["deterministic_resolution", "match_type:provider_id",
     "matched_values:{\"providerID\": \"gce://...\"}", "confidence:0.99"]

    Returns:
        (confidence_str, evidence_str) where confidence_str includes
        the label and type description, e.g. "HIGH (providerID exact match)",
        and evidence_str shows the matched values, e.g. 'providerID: "gce://..."'.
        Returns ("UNKNOWN", "") for missing/malformed input.
    """
    if not verified_via:
        return ("UNKNOWN", "")

    confidence_val = None
    match_type = None
    matched_values_raw = None

    for item in verified_via:
        if item.startswith("confidence:"):
            with contextlib.suppress(ValueError, IndexError):
                confidence_val = float(item.split(":", 1)[1])
        elif item.startswith("match_type:"):
            match_type = item.split(":", 1)[1]
        elif item.startswith("matched_values:"):
            matched_values_raw = item.split(":", 1)[1]

    # Map to human-readable confidence label
    if confidence_val is not None and confidence_val >= 0.95:
        confidence_label = "HIGH"
    elif confidence_val is not None and confidence_val >= 0.7:
        confidence_label = "MEDIUM"
    else:
        confidence_label = "LOW"

    # Build type description
    type_descriptions = {
        "provider_id": "providerID exact match",
        "ip_address": "IP address match",
        "hostname": "hostname match",
    }
    type_desc = type_descriptions.get(match_type, match_type or "unknown")
    confidence_str = f"{confidence_label} ({type_desc})"

    # Build evidence summary from matched_values JSON
    evidence_str = ""
    if matched_values_raw:
        try:
            values = json.loads(matched_values_raw)
            parts = [f'{k}: "{v}"' for k, v in values.items()]
            evidence_str = ", ".join(parts)
        except (json.JSONDecodeError, AttributeError):
            evidence_str = matched_values_raw

    return confidence_str, evidence_str


def extract_entity_mentions(text: str) -> list[str]:
    """Extract potential entity names from text using regex heuristics.

    Finds quoted strings, hostname-like patterns, and resource-name patterns.
    Returns a deduplicated list preserving insertion order.

    Args:
        text: The user message or goal text to scan.

    Returns:
        List of unique entity mention strings.
    """
    mentions: list[str] = []

    # Find quoted strings (double and single quotes)
    mentions.extend(re.findall(r'"([^"]+)"', text))
    mentions.extend(re.findall(r"'([^']+)'", text))

    # Find hostname-like patterns (e.g., shop.example.com)
    mentions.extend(re.findall(r"\b[\w-]+(?:\.[\w-]+)+\b", text))

    # Find resource-name patterns (e.g., my-pod-123, web-server-01)
    mentions.extend(re.findall(r"\b[a-z][\w-]*[0-9]+[\w-]*\b", text, re.IGNORECASE))

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for m in mentions:
        if m.lower() not in seen:
            seen.add(m.lower())
            unique.append(m)

    return unique


def extract_key_attributes(raw_attrs: dict) -> str:
    """Extract key identifiers useful for API calls from raw entity attributes.

    Args:
        raw_attrs: Dictionary of raw entity attributes.

    Returns:
        Comma-separated string of key=value pairs, or empty string.
    """
    key_fields = ["vmid", "vm_id", "id", "node", "hostname", "ip", "namespace", "cluster"]
    found = []
    for key in key_fields:
        if raw_attrs.get(key):
            found.append(f"{key}={raw_attrs[key]}")
    return ", ".join(found[:4]) if found else ""
