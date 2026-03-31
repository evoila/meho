# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Intent Detection for MEHO Agent (TASK-87)

Lightweight pattern-based request type detection.
NO LLM call - just fast pattern matching for zero latency.

This module detects the TYPE of request to guide context injection:
- DATA_QUERY: "List VMs", "Show pods"
- DATA_RECALL: "What's the IP of vm-57?"
- DATA_REFORMAT: "Format as table"
- ACTION: "Shut down vm-57", "Delete pod"
- KNOWLEDGE: "How do I troubleshoot X?"

Key principle: Detection GUIDES context injection but doesn't RESTRICT tools.
The agent always has access to all tools - we just provide better context.
"""

from dataclasses import dataclass
from enum import StrEnum

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


class RequestType(StrEnum):
    """
    Types of user requests.

    Used to customize context injection - NOT to restrict tools.
    """

    DATA_QUERY = "data_query"  # Retrieve/list data
    DATA_RECALL = "data_recall"  # Answer from cached data
    DATA_REFORMAT = "data_reformat"  # Transform/format cached data
    ACTION = "action"  # Execute an operation
    KNOWLEDGE = "knowledge"  # Search documentation
    UNKNOWN = "unknown"  # Let LLM decide


@dataclass
class DetectionResult:
    """Result of request type detection."""

    request_type: RequestType
    confidence: str  # "high", "medium", "low"
    matched_pattern: str | None = None
    reasoning: str = ""


# =============================================================================
# GENERIC PATTERNS (No system-specific keywords!)
# =============================================================================

# Action verbs that indicate the user wants to PERFORM an operation
ACTION_VERBS: set[str] = {
    # Lifecycle operations
    "start",
    "stop",
    "restart",
    "reboot",
    "shutdown",
    "shut down",
    "power off",
    "power on",
    "boot",
    "halt",
    "terminate",
    "kill",
    # CRUD operations
    "create",
    "delete",
    "remove",
    "destroy",
    "add",
    "insert",
    "update",
    "modify",
    "edit",
    "patch",
    "change",
    "set",
    "configure",
    # Deployment/scaling
    "deploy",
    "undeploy",
    "redeploy",
    "scale",
    "resize",
    "expand",
    "shrink",
    "provision",
    "deprovision",
    "spawn",
    "launch",
    # Data operations
    "migrate",
    "move",
    "copy",
    "clone",
    "backup",
    "restore",
    "sync",
    "refresh",
    "reset",
    "clear",
    "purge",
    "flush",
    "wipe",
    # State changes
    "enable",
    "disable",
    "activate",
    "deactivate",
    "suspend",
    "resume",
    "pause",
    "unpause",
    "lock",
    "unlock",
    "freeze",
    "unfreeze",
    # Access/permissions
    "grant",
    "revoke",
    "assign",
    "unassign",
    "attach",
    "detach",
    "connect",
    "disconnect",
    "mount",
    "unmount",
    # Network operations
    "open",
    "close",
    "block",
    "unblock",
    "allow",
    "deny",
    # Execution
    "run",
    "execute",
    "trigger",
    "invoke",
    "call",
    "fire",
}

# Action phrases that indicate creation/action (more specific)
ACTION_PHRASES: list[str] = [
    "give me a new",
    "get me a new",
    "make a new",
    "make me a",
    "create a",
    "spin up",
    "bring up",
    "bring down",
    "take down",
    "set up",
    "tear down",
    "roll back",
    "roll out",
    "switch on",
    "switch off",
    "turn on",
    "turn off",
]

# Data query patterns - retrieving existing data
DATA_QUERY_PATTERNS: list[str] = [
    # List/enumerate
    "list",
    "show me",
    "display",
    "get all",
    "fetch all",
    "retrieve all",
    "what are the",
    "what are all",
    "which are",
    "how many",
    "count",
    # Status/info requests
    "status of",
    "state of",
    "info about",
    "details of",
    "details for",
    "check the",
    "verify the",
    "inspect",
    # Simple retrieval (existing data)
    "get the",
    "give me the",
    "show me the",
    "tell me the",
    "what is the",
    "what's the",
    "what are the",
]

# Reformat patterns - transforming cached data
REFORMAT_PATTERNS: list[str] = [
    # Format conversion
    "format",
    "as a table",
    "as table",
    "as csv",
    "as json",
    "as markdown",
    "in table",
    "in csv",
    "in json",
    "to table",
    "to csv",
    "to json",
    "into a table",
    "into csv",
    # Display preferences
    "show as",
    "display as",
    "convert to",
    "export as",
    "export to",
    "present as",
    "render as",
    # Summarization
    "summarize",
    "summary of",
    "overview of",
    # Filtering/sorting (on existing data)
    "just the names",
    "only the",
    "just show",
    "filter to",
    "sort by",
    "order by",
    "group by",
    "sorted by",
]

# Knowledge patterns - documentation/how-to questions
# NOTE: "what are the X" is DATA_QUERY, "what are the steps" is KNOWLEDGE
# Be specific to avoid conflicts
KNOWLEDGE_PATTERNS: list[str] = [
    "how do i",
    "how to",
    "how can i",
    "how should i",
    "what is the process",
    "what are the steps",
    "steps to",
    "explain how",
    "guide me",
    "help me understand",
    "walk me through",
    "best practice",
    "recommended way",
    "proper way",
    "troubleshoot",
    "debug",
    "diagnose",
    "why is",
    "why does",
    "why can't",
    "why won't",
    "what does",
    "what is a",  # Note: removed "what are" - too generic
    "documentation",
    "docs for",
    "help with",
]


def _is_word_boundary_match(text: str, word: str) -> bool:
    """
    Check if 'word' appears as a standalone word in 'text' (not as substring).

    Examples:
        _is_word_boundary_match("show running pods", "run") -> False (part of "running")
        _is_word_boundary_match("run the job", "run") -> True
        _is_word_boundary_match("open pull requests", "open") -> True (adjective, but matched)
    """
    import re

    # Use word boundaries to match complete words only
    pattern = r"\b" + re.escape(word) + r"\b"
    return bool(re.search(pattern, text))


def detect_request_type(message: str) -> DetectionResult:
    """
    Detect the type of user request using lightweight pattern matching.

    This is FAST (no LLM call) and GENERIC (no system-specific keywords).

    Priority order (most specific to least specific):
    1. KNOWLEDGE - How-to/documentation questions
    2. ACTION - Action verbs/phrases
    3. DATA_QUERY - Generic list/show patterns
    4. UNKNOWN - Let LLM decide

    Args:
        message: User's message

    Returns:
        DetectionResult with request_type and confidence
    """
    msg = message.lower().strip()

    # ==========================================================================
    # 1. KNOWLEDGE - Questions about HOW to do things
    # Check before generic queries to catch documentation/how-to questions
    # ==========================================================================
    for pattern in KNOWLEDGE_PATTERNS:
        if pattern in msg:
            logger.debug(f"Detected KNOWLEDGE request: matched '{pattern}'")
            return DetectionResult(
                request_type=RequestType.KNOWLEDGE,
                confidence="high",
                matched_pattern=pattern,
                reasoning="Question about how to do something",
            )

    # ==========================================================================
    # 2. ACTION - Check for action verbs and phrases
    # ==========================================================================
    # Check specific phrases first (more reliable)
    for phrase in ACTION_PHRASES:
        if phrase in msg:
            logger.debug(f"Detected ACTION request: matched phrase '{phrase}'")
            return DetectionResult(
                request_type=RequestType.ACTION,
                confidence="high",
                matched_pattern=phrase,
                reasoning="Action phrase detected - user wants to perform operation",
            )

    # Ambiguous verbs that could be adjectives or part of other words

    # Contextual exclusions - if these phrases are present, don't match the verb
    EXCLUSION_CONTEXTS = {
        "sync": ["out of sync", "in sync", "sync status"],
        "open": ["open issues", "open pull", "open tickets", "open requests"],
        "close": ["closed issues", "closed pull", "closed tickets"],
    }

    # Check action verbs
    for verb in ACTION_VERBS:
        # Skip get/give - handled by phrases
        if verb in ("get", "give"):
            continue

        # Check if verb is in exclusion context
        if verb in EXCLUSION_CONTEXTS:  # noqa: SIM102 -- readability preferred over collapse
            if any(exc in msg for exc in EXCLUSION_CONTEXTS[verb]):
                continue

        # Require word boundary match for all verbs
        # This prevents "running" matching "run", "stopped" matching "stop", etc.
        if not _is_word_boundary_match(msg, verb):
            continue

        logger.debug(f"Detected ACTION request: matched verb '{verb}'")
        return DetectionResult(
            request_type=RequestType.ACTION,
            confidence="high",
            matched_pattern=verb,
            reasoning=f"Action verb '{verb}' detected - user wants to perform operation",
        )

    # ==========================================================================
    # 3. DATA_QUERY - Generic list/show patterns (most general)
    # ==========================================================================
    for pattern in DATA_QUERY_PATTERNS:
        if pattern in msg:
            logger.debug(f"Detected DATA_QUERY request: matched '{pattern}'")
            return DetectionResult(
                request_type=RequestType.DATA_QUERY,
                confidence="high",
                matched_pattern=pattern,
                reasoning="Request to retrieve/list data",
            )

    # ==========================================================================
    # 4. UNKNOWN - Let LLM decide
    # ==========================================================================
    logger.debug("Could not detect request type, returning UNKNOWN")
    return DetectionResult(
        request_type=RequestType.UNKNOWN,
        confidence="low",
        matched_pattern=None,
        reasoning="No clear pattern detected - LLM will decide",
    )


# =============================================================================
# Legacy compatibility (TASK-87 update)
# =============================================================================


# Old Intent enum for backward compatibility
class Intent(StrEnum):
    """Legacy intent enum - use RequestType instead."""

    REFORMAT = "REFORMAT"
    RECALL = "RECALL"
    FETCH_SINGLE = "FETCH_SINGLE"
    FETCH_BATCH = "FETCH_BATCH"
    CLARIFY = "CLARIFY"
    SEARCH = "SEARCH"
    SWITCH_SYSTEM = "SWITCH_SYSTEM"
    ACTION = "ACTION"  # Added in TASK-87 update


# Mapping from RequestType to legacy Intent
REQUEST_TYPE_TO_INTENT = {
    RequestType.DATA_QUERY: Intent.FETCH_BATCH,
    RequestType.DATA_RECALL: Intent.RECALL,
    RequestType.DATA_REFORMAT: Intent.REFORMAT,
    RequestType.ACTION: Intent.ACTION,
    RequestType.KNOWLEDGE: Intent.SEARCH,
    RequestType.UNKNOWN: Intent.FETCH_SINGLE,
}


# Tool recommendations by intent (informational only - not enforced)
TOOLS_BY_INTENT = {
    Intent.REFORMAT: ["reduce_data", "interpret_results"],
    Intent.RECALL: ["reduce_data"],
    Intent.FETCH_SINGLE: ["determine_connector", "search_endpoints", "call_endpoint"],
    Intent.FETCH_BATCH: ["determine_connector", "search_endpoints", "call_endpoint"],
    Intent.CLARIFY: [],
    Intent.SEARCH: ["search_docs", "search_endpoints"],
    Intent.SWITCH_SYSTEM: ["determine_connector", "list_connectors"],
    Intent.ACTION: ["search_endpoints", "call_endpoint", "reduce_data"],  # ACTION tools
}


def get_available_tools(intent: Intent) -> list[str]:
    """Get recommended tools for a given intent (informational only)."""
    return TOOLS_BY_INTENT.get(intent, [])
