# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Network diagnostics topology schema definition.

Defines entity types and valid relationships for network diagnostic probes.
These entities are emitted by the diagnostic tools (dns_resolve, http_probe,
tls_check) and serve as topology breadcrumbs for future investigations.

Entity Types:
- ExternalURL (unscoped, moderate volatility)
- IPAddress (unscoped, moderate volatility)
- TLSCertificate (unscoped, stable volatility)

Relationship:
- ExternalURL resolves_to IPAddress (many-to-many via DNS)
"""

from .base import (
    ConnectorTopologySchema,
    EntityTypeDefinition,
    RelationshipRule,
    SameAsEligibility,
    Volatility,
)

# =============================================================================
# Entity Type Definitions
# =============================================================================

_EXTERNAL_URL = EntityTypeDefinition(
    name="ExternalURL",
    scoped=False,
    identity_fields=["url"],
    volatility=Volatility.MODERATE,
    same_as=SameAsEligibility(
        can_match=["Ingress", "Service", "LoadBalancer", "CloudRunService"],
        matching_attributes=["url", "hostname"],
    ),
    navigation_hints=[
        "Discovered by http_probe tool",
        "May resolve to one or more IPAddress entities via DNS",
        "Check SAME_AS for matching Ingress/Service/LoadBalancer in connected infrastructure",
    ],
    common_queries=[
        "What IP addresses does this URL resolve to?",
        "Is this URL reachable?",
        "What infrastructure serves this URL?",
    ],
)

_IP_ADDRESS = EntityTypeDefinition(
    name="IPAddress",
    scoped=False,
    identity_fields=["ip"],
    volatility=Volatility.MODERATE,
    same_as=SameAsEligibility(
        can_match=["VM", "Instance", "Node", "Host", "LoadBalancer", "EC2Instance"],
        matching_attributes=["ip"],
    ),
    navigation_hints=[
        "Discovered by dns_resolve tool",
        "May be resolved from ExternalURL entities",
        "Check SAME_AS for matching VM/Instance/Node in connected infrastructure",
    ],
    common_queries=[
        "What hostnames resolve to this IP?",
        "What infrastructure owns this IP?",
        "Is this IP reachable on expected ports?",
    ],
)

_TLS_CERTIFICATE = EntityTypeDefinition(
    name="TLSCertificate",
    scoped=False,
    identity_fields=["hostname", "port"],
    volatility=Volatility.STABLE,
    same_as=None,  # Certificates don't correlate cross-connector
    navigation_hints=[
        "Discovered by tls_check tool",
        "Contains certificate subject, issuer, expiry, and SANs",
        "No cross-connector correlation — certificates are endpoint-specific",
    ],
    common_queries=[
        "When does this certificate expire?",
        "Is the certificate chain valid?",
        "What SANs does this certificate cover?",
    ],
)

# =============================================================================
# Relationship Rules
# =============================================================================

_RESOLUTION_RULES = {
    ("ExternalURL", "resolves_to", "IPAddress"): RelationshipRule(
        from_type="ExternalURL",
        relationship_type="resolves_to",
        to_type="IPAddress",
        cardinality="many_to_many",
    ),
}

# =============================================================================
# Complete Network Diagnostics Schema
# =============================================================================

NETWORK_DIAGNOSTICS_TOPOLOGY_SCHEMA = ConnectorTopologySchema(
    connector_type="network_diagnostics",
    entity_types={
        "ExternalURL": _EXTERNAL_URL,
        "IPAddress": _IP_ADDRESS,
        "TLSCertificate": _TLS_CERTIFICATE,
    },
    relationship_rules={
        **_RESOLUTION_RULES,
    },
)
