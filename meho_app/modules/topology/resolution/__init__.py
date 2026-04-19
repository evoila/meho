# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Deterministic entity resolution engine.

Provides attribute-based matchers (ProviderID, IP, Hostname) orchestrated
by a DeterministicResolver that applies them in priority order.

Usage:
    from meho_app.modules.topology.resolution import get_default_resolver

    resolver = get_default_resolver()
    evidence = resolver.resolve_pair(entity_a, entity_b)
    if evidence and evidence.auto_confirm:
        # Create confirmed SAME_AS relationship
        ...

Public API:
    get_default_resolver() -> DeterministicResolver
    DeterministicResolver
    MatchEvidence
    MatchPriority
"""

from meho_app.modules.topology.resolution.evidence import MatchEvidence, MatchPriority
from meho_app.modules.topology.resolution.matchers.hostname import HostnameMatcher
from meho_app.modules.topology.resolution.matchers.ip_address import IPAddressMatcher
from meho_app.modules.topology.resolution.matchers.provider_id import ProviderIDMatcher
from meho_app.modules.topology.resolution.resolver import DeterministicResolver


def get_default_resolver() -> DeterministicResolver:
    """
    Get a pre-configured DeterministicResolver with all matchers.

    Returns a resolver with ProviderIDMatcher, IPAddressMatcher, and
    HostnameMatcher in priority order (providerID > IP > hostname).
    """
    return DeterministicResolver(
        matchers=[
            ProviderIDMatcher(),
            IPAddressMatcher(),
            HostnameMatcher(),
        ]
    )


__all__ = [
    "DeterministicResolver",
    "HostnameMatcher",
    "IPAddressMatcher",
    "MatchEvidence",
    "MatchPriority",
    "ProviderIDMatcher",
    "get_default_resolver",
]
