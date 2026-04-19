# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Entity extractor for user messages.

Extracts potential entity references from user messages so we can
look them up in the topology database before the agent starts reasoning.

Examples of extracted entities:
- URLs: shop.example.com, api.company.io
- Quoted strings: "shop-frontend", 'node-01'
- Known patterns: pod/shop-frontend, vm/k8s-worker-01
- IP addresses: 192.168.1.10
- K8s-style names: shop-frontend-deployment-abc123
"""

import re

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


class EntityExtractor:
    """
    Extracts potential entity references from user messages.

    The goal is to find anything that might be an entity name so we can
    look it up in the topology database. False positives are OK (we'll
    just get "not found" from lookup), but we want high recall.

    Usage:
        extractor = EntityExtractor()
        refs = extractor.extract("My website shop.example.com is slow")
        # Returns: ["shop.example.com"]
    """

    # Common infrastructure entity prefixes
    KNOWN_PREFIXES = [  # noqa: RUF012 -- mutable default is intentional class state
        "pod/",
        "pods/",
        "deployment/",
        "deployments/",
        "service/",
        "services/",
        "svc/",
        "ingress/",
        "node/",
        "nodes/",
        "vm/",
        "host/",
        "cluster/",
        "namespace/",
        "ns/",
        "storage/",
        "volume/",
        "pv/",
        "pvc/",
    ]

    # Patterns that look like K8s/infra resource names
    # e.g., shop-frontend-deployment-7b9f4c8d5c-xk9m2
    K8S_NAME_PATTERN = re.compile(
        r"\b([a-z][a-z0-9-]{2,}(?:-[a-z0-9]{4,10}){0,3})\b", re.IGNORECASE
    )

    # URL/hostname pattern
    URL_PATTERN = re.compile(
        r"\b([a-zA-Z0-9][-a-zA-Z0-9]*\.(?:[a-zA-Z]{2,}|[a-zA-Z0-9][-a-zA-Z0-9]*\.)+[a-zA-Z]{2,})\b"
    )

    # Simple hostname pattern (single-level, like "node-01")
    HOSTNAME_PATTERN = re.compile(r"\b([a-z][a-z0-9-]*-\d+)\b", re.IGNORECASE)

    # IP address pattern
    IP_PATTERN = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")

    # Quoted string pattern
    QUOTED_PATTERN = re.compile(r'["\']([^"\']+)["\']')

    # Words to exclude (common English words that might match patterns)
    EXCLUDE_WORDS = {  # noqa: RUF012 -- mutable default is intentional class state
        "the",
        "and",
        "for",
        "are",
        "but",
        "not",
        "you",
        "all",
        "can",
        "had",
        "her",
        "was",
        "one",
        "our",
        "out",
        "has",
        "his",
        "how",
        "its",
        "let",
        "may",
        "new",
        "now",
        "old",
        "see",
        "way",
        "who",
        "did",
        "get",
        "got",
        "him",
        "she",
        "too",
        "use",
        "what",
        "why",
        "when",
        "where",
        "which",
        "this",
        "that",
        "with",
        "have",
        "from",
        "they",
        "been",
        "would",
        "could",
        "should",
        "about",
        "after",
        "before",
        "slow",
        "down",
        "fast",
        "high",
        "check",
        "help",
        "error",
        "issue",
        "problem",
        "status",
        "please",
        "thanks",
    }

    def __init__(
        self,
        min_length: int = 3,
        max_length: int = 100,
    ) -> None:
        """
        Initialize the extractor.

        Args:
            min_length: Minimum entity reference length
            max_length: Maximum entity reference length
        """
        self.min_length = min_length
        self.max_length = max_length

    def extract(self, message: str) -> list[str]:
        """
        Extract potential entity references from a message.

        Args:
            message: User message to extract from

        Returns:
            List of unique potential entity references
        """
        if not message:
            return []

        refs: set[str] = set()

        # 1. Extract URLs/hostnames
        urls = self.URL_PATTERN.findall(message)
        refs.update(urls)

        # 2. Extract simple hostnames (node-01, worker-02)
        hostnames = self.HOSTNAME_PATTERN.findall(message)
        refs.update(hostnames)

        # 3. Extract IP addresses
        ips = self.IP_PATTERN.findall(message)
        refs.update(ips)

        # 4. Extract quoted strings
        quoted = self.QUOTED_PATTERN.findall(message)
        refs.update(q.strip() for q in quoted if q.strip())

        # 5. Extract known prefix patterns (pod/name, vm/name, etc.)
        for prefix in self.KNOWN_PREFIXES:
            pattern = re.compile(rf"{re.escape(prefix)}([a-zA-Z0-9][-a-zA-Z0-9_.]*)", re.IGNORECASE)
            matches = pattern.findall(message)
            refs.update(matches)

        # 6. Extract K8s-style names
        k8s_names = self.K8S_NAME_PATTERN.findall(message)
        for name in k8s_names:
            # Filter out common words and too-short names
            name_lower = name.lower()
            if (
                len(name) >= self.min_length
                and name_lower not in self.EXCLUDE_WORDS
                and "-" in name  # Must have at least one hyphen for K8s names
            ):
                refs.add(name)

        # Filter and deduplicate
        result = [ref for ref in refs if self.min_length <= len(ref) <= self.max_length]

        # Remove duplicates while preserving order
        seen = set()
        unique_result = []
        for ref in result:
            ref_lower = ref.lower()
            if ref_lower not in seen:
                seen.add(ref_lower)
                unique_result.append(ref)

        logger.debug(f"Extracted {len(unique_result)} entity references from message")

        return unique_result

    def extract_with_context(
        self,
        message: str,
    ) -> list[dict[str, str]]:
        """
        Extract entity references with context about how they were found.

        Args:
            message: User message to extract from

        Returns:
            List of dicts with 'reference' and 'type' keys
        """
        if not message:
            return []

        refs: list[dict[str, str]] = []
        seen: set[str] = set()

        def add_ref(reference: str, ref_type: str) -> None:
            ref_lower = reference.lower()
            if ref_lower not in seen and self.min_length <= len(reference) <= self.max_length:
                seen.add(ref_lower)
                refs.append({"reference": reference, "type": ref_type})

        # Extract with type information
        for url in self.URL_PATTERN.findall(message):
            add_ref(url, "url")

        for hostname in self.HOSTNAME_PATTERN.findall(message):
            add_ref(hostname, "hostname")

        for ip in self.IP_PATTERN.findall(message):
            add_ref(ip, "ip_address")

        for quoted in self.QUOTED_PATTERN.findall(message):
            if quoted.strip():
                add_ref(quoted.strip(), "quoted")

        for prefix in self.KNOWN_PREFIXES:
            pattern = re.compile(rf"{re.escape(prefix)}([a-zA-Z0-9][-a-zA-Z0-9_.]*)", re.IGNORECASE)
            for match in pattern.findall(message):
                add_ref(match, f"prefixed:{prefix.rstrip('/')}")

        return refs


# =============================================================================
# Convenience function
# =============================================================================

_extractor: EntityExtractor | None = None


def get_entity_extractor() -> EntityExtractor:
    """Get the entity extractor singleton."""
    global _extractor
    if _extractor is None:
        _extractor = EntityExtractor()
    return _extractor


def extract_entity_references(message: str) -> list[str]:
    """
    Convenience function to extract entity references from a message.

    Args:
        message: User message

    Returns:
        List of potential entity references
    """
    return get_entity_extractor().extract(message)
