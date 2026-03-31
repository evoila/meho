# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Trust Classification Pipeline.

Phase 5: Unified classification that checks per-endpoint overrides first,
then falls back to typed connector registry, operation name heuristic,
HTTP method heuristic, or default WRITE.

Priority chain:
  1. Per-endpoint override (from DB, passed as parameter)
  2. Typed connector registry (static map for kubernetes, vmware, proxmox, gcp)
  3. Operation name heuristic (prefix-based: list_/get_ -> READ, delete_ -> DESTRUCTIVE)
  4. HTTP method heuristic (GET/HEAD/OPTIONS -> READ, POST/PUT/PATCH -> WRITE, DELETE -> DESTRUCTIVE)
  5. Default -> WRITE (fail-safe)
"""

from __future__ import annotations

from meho_app.modules.agents.approval.trust_registry import get_tier
from meho_app.modules.agents.models import TrustTier

_READ_PREFIXES = (
    "list_",
    "get_",
    "describe_",
    "search_",
    "browse_",
    "query_",
    "export_",
    "find_",
    "retrieve_",
    "acquire_",
    "download_",
    "place_",
    "recommend_",
)

_DESTRUCTIVE_PREFIXES = (
    "delete_",
    "destroy_",
    "remove_",
    "unregister_",
)


def _classify_by_name(operation_id: str) -> TrustTier | None:
    """Classify by operation_id naming convention.

    Covers the long tail of typed connector operations that aren't in the
    static registry. Returns None when no prefix matches so the caller
    falls through to the next heuristic.
    """
    op = operation_id.lower()
    if any(op.startswith(p) for p in _READ_PREFIXES):
        return TrustTier.READ
    if any(op.startswith(p) for p in _DESTRUCTIVE_PREFIXES):
        return TrustTier.DESTRUCTIVE
    return None


def classify_operation(
    connector_type: str,
    operation_id: str,
    http_method: str | None = None,
    override: TrustTier | None = None,
) -> TrustTier:
    """Classify an operation into a trust tier.

    Implements the priority chain:
      per-endpoint override > static tier map > name heuristic
      > HTTP method heuristic > default WRITE

    Args:
        connector_type: Connector type (e.g., "rest", "kubernetes", "vmware").
        operation_id: Operation identifier (e.g., "list_pods", "create_user").
        http_method: HTTP method for REST connectors (e.g., "GET", "POST").
        override: Per-endpoint override from DB (takes highest priority).

    Returns:
        TrustTier classification for the operation.
    """
    # 1. Per-endpoint override (from DB)
    if override is not None:
        return override

    # 2. Typed connector: static map
    if connector_type.lower() != "rest":
        tier = get_tier(connector_type, operation_id)
        if tier is not None:
            return tier

    # 3. Operation name heuristic (prefix-based)
    tier = _classify_by_name(operation_id)
    if tier is not None:
        return tier

    # 4. REST: HTTP method heuristic
    if http_method is not None:
        method = http_method.upper()
        if method in ("GET", "HEAD", "OPTIONS"):
            return TrustTier.READ
        if method == "DELETE":
            return TrustTier.DESTRUCTIVE
        if method in ("POST", "PUT", "PATCH"):
            return TrustTier.WRITE
        return TrustTier.WRITE

    # 5. Default: WRITE (fail-safe)
    return TrustTier.WRITE


def safety_level_to_trust_tier(safety_level: str | None) -> TrustTier | None:
    """Convert a DB safety_level string to a TrustTier override.

    Returns None for "auto", "safe" (default, meaning no override), and unknown values.
    Maps both old vocabulary and new vocabulary:
      - "auto" -> None (use heuristic)
      - "safe" -> None (old default, no override)
      - "read" -> TrustTier.READ
      - "caution" -> TrustTier.WRITE
      - "dangerous" -> TrustTier.WRITE
      - "write" -> TrustTier.WRITE
      - "critical" -> TrustTier.DESTRUCTIVE
      - "destructive" -> TrustTier.DESTRUCTIVE
    """
    if not safety_level or safety_level in ("auto", "safe"):
        return None

    mapping = {
        "read": TrustTier.READ,
        "caution": TrustTier.WRITE,
        "dangerous": TrustTier.WRITE,
        "write": TrustTier.WRITE,
        "critical": TrustTier.DESTRUCTIVE,
        "destructive": TrustTier.DESTRUCTIVE,
    }
    return mapping.get(safety_level.lower())


def requires_approval(tier: TrustTier) -> bool:
    """Check if a trust tier requires operator approval.

    READ operations are auto-approved. WRITE and DESTRUCTIVE require
    the operator to explicitly approve before execution.

    Args:
        tier: The trust tier to check.

    Returns:
        True if the tier requires operator approval.
    """
    return tier != TrustTier.READ
