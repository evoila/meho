# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Hostname matcher for deterministic entity resolution.

Matches entities by comparing normalized hostnames. Strips known domain
suffixes (.internal, .local, .localdomain, .compute.googleapis.com, etc.)
and performs case-insensitive comparison.

Integrates entity-type-aware extraction patterns from the existing
HostnameMatcher service:
- K8s Ingress: spec.rules[].host
- VMware VM: guest.hostname, _extracted_hostname
- GCP Instance: name
- REST/SOAP: base_url hostname via urllib.parse

Hostname comparison is case-insensitive.
Exact match after normalization -> auto_confirm=True, confidence=0.95.
"""

import re
from urllib.parse import urlparse

from meho_app.modules.topology.models import TopologyEntityModel
from meho_app.modules.topology.resolution.evidence import MatchEvidence, MatchPriority
from meho_app.modules.topology.resolution.matchers.base import BaseMatcher

# Known domain suffixes to strip during normalization.
# Order matters: more specific patterns first.
_STRIP_SUFFIXES_FIXED = [
    ".compute.googleapis.com",
    ".compute.internal",
    ".localdomain",
    ".internal",
    ".local",
]

# Regex pattern for GCP internal DNS: hostname.ZONE.c.PROJECT.internal
_GCP_INTERNAL_PATTERN = re.compile(
    r"\.[a-z0-9-]+\.c\.[a-z0-9-]+\.internal$",
    re.IGNORECASE,
)


def normalize_hostname(hostname: str) -> str:
    """
    Normalize hostname by stripping known domain suffixes.

    Examples:
        "node-01.internal" -> "node-01"
        "gke-cluster-abc.us-central1-a.c.myproject.internal" -> "gke-cluster-abc"
        "worker-01.local" -> "worker-01"
        "worker-01" -> "worker-01" (no change)
        "node.compute.googleapis.com" -> "node"
    """
    normalized = hostname.strip().lower()

    # Strip GCP internal DNS pattern first (most specific)
    normalized = _GCP_INTERNAL_PATTERN.sub("", normalized)

    # Iteratively strip fixed suffixes
    changed = True
    while changed:
        changed = False
        for suffix in _STRIP_SUFFIXES_FIXED:
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)]
                changed = True
                break  # Restart from the beginning after a strip

    return normalized


class HostnameMatcher(BaseMatcher):
    """
    Matches entities by comparing normalized hostnames.

    Uses the entity name as primary hostname, and also checks raw_attributes
    for additional hostname sources:
    - _extracted_hostname
    - guest.hostname (VMware)
    - spec.rules[].host (K8s Ingress)
    - base_url hostname (REST/SOAP connectors)
    """

    priority = MatchPriority.HOSTNAME

    def match(
        self,
        entity_a: TopologyEntityModel,
        entity_b: TopologyEntityModel,
    ) -> MatchEvidence | None:
        """Compare normalized hostnames between two entities."""
        hostnames_a = self._extract_hostnames(entity_a)
        hostnames_b = self._extract_hostnames(entity_b)

        if not hostnames_a or not hostnames_b:
            return None

        # Normalize all hostnames
        normalized_a = {normalize_hostname(h) for h in hostnames_a}
        normalized_b = {normalize_hostname(h) for h in hostnames_b}

        # Remove empty strings that might result from normalization
        normalized_a.discard("")
        normalized_b.discard("")

        if not normalized_a or not normalized_b:
            return None

        # Check for overlap
        overlap = normalized_a & normalized_b
        if not overlap:
            return None

        matched_hostname = next(iter(overlap))
        return MatchEvidence(
            match_type="hostname_exact",
            matched_values={
                "matched_hostname": matched_hostname,
                "entity_a_hostnames": sorted(hostnames_a),
                "entity_b_hostnames": sorted(hostnames_b),
            },
            confidence=0.95,
            auto_confirm=True,
        )

    def _extract_hostnames(self, entity: TopologyEntityModel) -> set[str]:
        """
        Extract all hostnames from entity name and raw_attributes.

        Returns a set of raw (un-normalized) hostname strings.
        """
        hostnames: set[str] = set()

        # Primary: entity name
        if entity.name:
            hostnames.add(entity.name)

        attrs = entity.raw_attributes or {}

        # _extracted_hostname
        extracted = attrs.get("_extracted_hostname")
        if extracted and isinstance(extracted, str):
            hostnames.add(extracted)

        # guest.hostname (VMware)
        guest = attrs.get("guest")
        if isinstance(guest, dict):
            guest_hostname = guest.get("hostname")
            if guest_hostname and isinstance(guest_hostname, str):
                hostnames.add(guest_hostname)

        # K8s Ingress: spec.rules[].host
        self._extract_ingress_hosts(attrs, hostnames)

        # REST/SOAP: base_url hostname
        self._extract_url_hostname(attrs, hostnames)

        return hostnames

    def _extract_ingress_hosts(self, attrs: dict, hostnames: set[str]) -> None:
        """Extract hosts from K8s Ingress spec.rules."""
        # Check if this looks like a K8s Ingress
        kind = attrs.get("kind")
        if kind != "Ingress":
            return

        spec = attrs.get("spec")
        if not isinstance(spec, dict):
            return

        rules = spec.get("rules")
        if not isinstance(rules, list):
            return

        for rule in rules:
            if isinstance(rule, dict):
                host = rule.get("host")
                if host and isinstance(host, str):
                    hostnames.add(host)

    def _extract_url_hostname(self, attrs: dict, hostnames: set[str]) -> None:
        """Extract hostname from base_url (REST/SOAP connectors)."""
        base_url = attrs.get("base_url")
        if not base_url or not isinstance(base_url, str):
            return

        try:
            parsed = urlparse(base_url)
            hostname = parsed.hostname
            if hostname and isinstance(hostname, str):
                # Only add if it looks like a hostname, not an IP
                # IPs are handled by IPAddressMatcher
                import ipaddress as _ipaddress

                try:
                    _ipaddress.ip_address(hostname)
                    # It's an IP -- skip, let IPAddressMatcher handle it
                    return
                except ValueError:
                    # It's a hostname -- add it
                    hostnames.add(hostname)
        except Exception:  # noqa: S110 -- intentional silent exception handling
            pass
