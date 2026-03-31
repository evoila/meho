# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
IP address matcher for deterministic entity resolution.

Matches entities across connectors by comparing IP addresses.
Extracts IPs from different formats per connector type:
- K8s Node: _extracted_addresses or status.addresses array
- VMware VM: _extracted_ip_address or guest.ip_address
- GCP Instance: _extracted_network_interfaces (networkIP, natIP)
- REST/SOAP: base_url parsed via urllib.parse

Uses stdlib ipaddress module for validation and comparison
(handles IPv6, leading zeros, etc.).
"""

import contextlib
import ipaddress
from urllib.parse import urlparse

from meho_app.modules.topology.models import TopologyEntityModel
from meho_app.modules.topology.resolution.evidence import MatchEvidence, MatchPriority
from meho_app.modules.topology.resolution.matchers.base import BaseMatcher


class IPAddressMatcher(BaseMatcher):
    """
    Matches entities by comparing IP addresses across connectors.

    Extracts all IP addresses from each entity's raw_attributes,
    then checks for set intersection. First overlapping IP produces
    a match with confidence 1.0 and auto_confirm=True.
    """

    priority = MatchPriority.IP_ADDRESS

    def match(
        self,
        entity_a: TopologyEntityModel,
        entity_b: TopologyEntityModel,
    ) -> MatchEvidence | None:
        """Compare IP addresses between two entities."""
        ips_a = self._extract_ips(entity_a)
        ips_b = self._extract_ips(entity_b)

        if not ips_a or not ips_b:
            return None

        # Find overlapping IPs using set intersection
        overlap = ips_a & ips_b
        if not overlap:
            return None

        matched_ip = next(iter(overlap))
        return MatchEvidence(
            match_type="ip_address",
            matched_values={
                "matched_ip": str(matched_ip),
            },
            confidence=1.0,
            auto_confirm=True,
        )

    def _extract_ips(
        self, entity: TopologyEntityModel
    ) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        """
        Extract all valid IP addresses from entity raw_attributes.

        Checks multiple locations depending on entity/connector type.
        Invalid IP strings are silently skipped.
        """
        attrs = entity.raw_attributes
        if not attrs:
            return set()

        ips: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()

        # K8s Node: _extracted_addresses or status.addresses
        self._extract_k8s_addresses(attrs, ips)

        # VMware VM: _extracted_ip_address or guest.ip_address
        self._extract_vmware_ips(attrs, ips)

        # GCP Instance: _extracted_network_interfaces or networkInterfaces
        self._extract_gcp_ips(attrs, ips)

        # REST/SOAP: base_url
        self._extract_url_ips(attrs, ips)

        return ips

    def _extract_k8s_addresses(self, attrs: dict, ips: set) -> None:
        """Extract IPs from K8s Node addresses array."""
        addresses = attrs.get("_extracted_addresses")
        if not addresses:
            status = attrs.get("status")
            if isinstance(status, dict):
                addresses = status.get("addresses", [])

        if not addresses or not isinstance(addresses, list):
            return

        for addr in addresses:
            if not isinstance(addr, dict):
                continue
            addr_type = addr.get("type", "")
            if addr_type in ("InternalIP", "ExternalIP"):
                self._safe_add_ip(addr.get("address", ""), ips)

    def _extract_vmware_ips(self, attrs: dict, ips: set) -> None:
        """Extract IPs from VMware VM attributes."""
        # Check extracted format
        ip_str = attrs.get("_extracted_ip_address")
        if ip_str:
            self._safe_add_ip(ip_str, ips)

        # Check guest info
        guest = attrs.get("guest")
        if isinstance(guest, dict):
            ip_str = guest.get("ip_address")
            if ip_str:
                self._safe_add_ip(ip_str, ips)

        # Check flat ip_address
        ip_str = attrs.get("ip_address")
        if ip_str:
            self._safe_add_ip(ip_str, ips)

    def _extract_gcp_ips(self, attrs: dict, ips: set) -> None:
        """Extract IPs from GCP Instance network interfaces."""
        interfaces = (
            attrs.get("_extracted_network_interfaces")
            or attrs.get("network_interfaces")
            or attrs.get("networkInterfaces")
            or []
        )

        if not isinstance(interfaces, list):
            return

        for iface in interfaces:
            if not isinstance(iface, dict):
                continue

            # Internal IP
            network_ip = iface.get("networkIP")
            if network_ip:
                self._safe_add_ip(network_ip, ips)

            # External IP (NAT)
            access_configs = iface.get("accessConfigs", [])
            if isinstance(access_configs, list):
                for config in access_configs:
                    if isinstance(config, dict):
                        nat_ip = config.get("natIP")
                        if nat_ip:
                            self._safe_add_ip(nat_ip, ips)

    def _extract_url_ips(self, attrs: dict, ips: set) -> None:
        """Extract IP addresses from base_url (REST/SOAP connectors)."""
        base_url = attrs.get("base_url")
        if not base_url or not isinstance(base_url, str):
            return

        try:
            parsed = urlparse(base_url)
            hostname = parsed.hostname
            if hostname:
                # Only add if it's actually an IP address, not a hostname
                self._safe_add_ip(hostname, ips)
        except Exception:  # noqa: S110 -- intentional silent exception handling
            pass

    def _safe_add_ip(self, ip_str: str, ips: set) -> None:
        """Add IP to set if valid, silently skip otherwise."""
        if not ip_str or not isinstance(ip_str, str):
            return
        with contextlib.suppress(ValueError, TypeError):
            ips.add(ipaddress.ip_address(ip_str.strip()))
