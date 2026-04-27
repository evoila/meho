# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Hostname matching service for automatic correlation detection.

Detects when discovered topology entities (K8s Ingresses, VMware VMs)
have hostnames/IPs that match REST/SOAP connector targets.

This enables automatic correlation suggestions like:
- K8s Ingress "shop-ingress" with host "api.myapp.com"
  ↔ REST connector targeting "https://api.myapp.com"

- VMware VM "web-01" with IP "192.168.1.10"
  ↔ REST connector targeting "http://192.168.1.10:8080"

Phase 3 Enhancement (TASK-144):
- High confidence (>= 0.90): Auto-approve
- Mid confidence (0.70-0.89): LLM verification
- Low confidence (< 0.70): Manual review only
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.config import get_config
from meho_app.core.otel import get_logger
from meho_app.modules.connectors.models import ConnectorModel
from meho_app.modules.connectors.repositories.connector_repository import (
    ConnectorRepository,
)

from .models import TopologyEntityModel, TopologySameAsSuggestionModel
from .repository import TopologyRepository
from .schemas import SameAsSuggestionCreate

logger = get_logger(__name__)


# Confidence scores for different match types
HOSTNAME_EXACT_MATCH_CONFIDENCE = 0.95
IP_EXACT_MATCH_CONFIDENCE = 0.90
PARTIAL_HOSTNAME_CONFIDENCE = 0.70


class HostnameMatcher:
    """
    Service for detecting hostname/IP correlations between entities and connectors.

    When a topology entity is discovered (e.g., K8s Ingress, VMware VM),
    this service checks if any connector targets the same hostname/IP.
    If a match is found, a SameAsSuggestion is created.

    Phase 3 Enhancement (TASK-144):
    - Suggestions with confidence >= auto_approve_threshold are auto-approved
    - Suggestions with confidence between llm_verify_threshold and auto_approve_threshold
      are verified using LLM analysis of entity attributes
    - Suggestions below llm_verify_threshold are left for manual review

    Usage:
        matcher = HostnameMatcher(session)

        # After storing a K8s Ingress entity
        suggestions = await matcher.check_entity_correlations(
            entity=ingress_entity,
            tenant_id="tenant-1",
        )
        # Returns list of created suggestions (may already be approved/verified)
    """

    def __init__(self, session: AsyncSession, enable_llm_verification: bool = True) -> None:
        self.session = session
        self.connector_repo = ConnectorRepository(session)
        self.topology_repo = TopologyRepository(session)
        self._config = get_config()
        self._enable_llm_verification = enable_llm_verification

    async def find_matching_connectors(
        self,
        hostname: str,
        tenant_id: str,
    ) -> list[ConnectorModel]:
        """
        Find connectors whose base_url targets the given hostname.

        Args:
            hostname: Hostname or IP to search for
            tenant_id: Tenant ID

        Returns:
            List of matching connectors
        """
        return await self.connector_repo.find_by_target_host(
            hostname=hostname,
            tenant_id=tenant_id,
            active_only=True,
        )

    async def check_entity_correlations(  # NOSONAR (cognitive complexity)
        self,
        entity: TopologyEntityModel,
        tenant_id: str,
    ) -> list[TopologySameAsSuggestionModel]:
        """
        Check if an entity correlates with any connector targets.

        Extracts hostnames/IPs from the entity's raw_attributes and
        checks if any connector targets them.

        Args:
            entity: The topology entity to check
            tenant_id: Tenant ID

        Returns:
            List of created suggestions
        """
        # Extract hostnames/IPs from entity based on type
        hostnames = self._extract_hostnames_from_entity(entity)

        if not hostnames:
            return []

        created_suggestions = []

        for hostname, match_type in hostnames:
            # Find connectors targeting this hostname
            matching_connectors = await self.find_matching_connectors(
                hostname=hostname,
                tenant_id=tenant_id,
            )

            for connector in matching_connectors:
                # Skip if connector has no topology entity
                if not connector.topology_entity_id:
                    logger.debug(f"Skipping connector {connector.name} - no topology entity")
                    continue

                # Cast connector topology_entity_id (Column[UUID] -> UUID)
                topo_entity_id = UUID(str(connector.topology_entity_id))

                # Skip if same connector (entity came from this connector)
                if entity.connector_id == connector.id:
                    continue

                # Check if suggestion or SAME_AS already exists
                existing_suggestion = await self.topology_repo.get_existing_suggestion(
                    entity_a_id=entity.id,
                    entity_b_id=topo_entity_id,
                )
                if existing_suggestion:
                    logger.debug(f"Suggestion already exists for {entity.name} ↔ {connector.name}")
                    continue

                existing_same_as = await self.topology_repo.check_existing_same_as(
                    entity_a_id=entity.id,
                    entity_b_id=topo_entity_id,
                )
                if existing_same_as:
                    logger.debug(f"SAME_AS already exists for {entity.name} ↔ {connector.name}")
                    continue

                # Determine confidence based on match type
                confidence = self._get_confidence_for_match_type(match_type)

                # Create suggestion
                suggestion_input = SameAsSuggestionCreate(
                    entity_a_id=entity.id,
                    entity_b_id=topo_entity_id,
                    confidence=confidence,
                    match_type=match_type,
                    match_details=f"Entity '{entity.name}' hostname '{hostname}' matches connector '{connector.name}' target",
                )

                suggestion = await self.topology_repo.create_suggestion(
                    suggestion=suggestion_input,
                    tenant_id=tenant_id,
                )
                created_suggestions.append(suggestion)

                logger.info(
                    f"Created correlation suggestion: {entity.name} ↔ {connector.name} "
                    f"(match_type={match_type}, confidence={confidence})"
                )

                # Phase 3: Handle based on confidence thresholds
                await self._handle_suggestion_by_confidence(suggestion, confidence)

        return created_suggestions

    async def _handle_suggestion_by_confidence(
        self,
        suggestion: TopologySameAsSuggestionModel,
        confidence: float,
    ) -> None:
        """
        Handle a newly created suggestion based on its confidence score.

        - High confidence (>= auto_approve_threshold): Auto-approve
        - Mid confidence (>= llm_verify_threshold): LLM verification
        - Low confidence: Leave for manual review
        """
        auto_approve_threshold = self._config.suggestion_auto_approve_threshold
        llm_verify_threshold = self._config.suggestion_llm_verify_threshold

        if confidence >= auto_approve_threshold:
            # High confidence: auto-approve
            await self.topology_repo.approve_suggestion(
                suggestion_id=suggestion.id,
                user_id="auto_approve_high_confidence",
            )
            logger.info(
                f"Auto-approved suggestion {suggestion.id} "
                f"(confidence={confidence:.2f} >= {auto_approve_threshold})"
            )

        elif confidence >= llm_verify_threshold and self._enable_llm_verification:
            # Mid confidence: trigger LLM verification
            try:
                from .suggestion_verifier import SuggestionVerifier

                verifier = SuggestionVerifier(self.session)
                new_status = await verifier.process_and_resolve(suggestion.id)

                logger.info(
                    f"LLM verification for suggestion {suggestion.id}: "
                    f"confidence={confidence:.2f}, result={new_status}"
                )
            except Exception as e:
                logger.warning(
                    f"LLM verification failed for suggestion {suggestion.id}: {e}. "
                    "Leaving for manual review."
                )

        else:
            # Low confidence: manual review
            logger.debug(
                f"Suggestion {suggestion.id} left for manual review "
                f"(confidence={confidence:.2f} < {llm_verify_threshold})"
            )

    def _extract_hostnames_from_entity(
        self,
        entity: TopologyEntityModel,
    ) -> list[tuple[str, str]]:
        """
        Extract hostnames and IPs from an entity's raw_attributes.

        Returns list of (hostname, match_type) tuples.
        """
        if not entity.raw_attributes:
            return []

        attrs = entity.raw_attributes
        hostnames = []

        # Check entity description for type hints
        description = entity.description.lower() if entity.description else ""

        # K8s Ingress: spec.rules[].host
        if "ingress" in description or attrs.get("kind") == "Ingress":
            hostnames.extend(self._extract_k8s_ingress_hosts(attrs))

        # K8s Service with LoadBalancer: status.loadBalancer.ingress[].hostname/ip
        elif "service" in description or attrs.get("kind") == "Service":
            hostnames.extend(self._extract_k8s_service_hosts(attrs))

        # VMware VM: guest.ip_address, guest.hostname
        elif "vm" in description or "virtual machine" in description:
            hostnames.extend(self._extract_vmware_vm_hosts(attrs))

        # GCP Instance: networkInterfaces[].accessConfigs[].natIP
        elif "instance" in description or "gcp" in description:
            hostnames.extend(self._extract_gcp_instance_hosts(attrs))

        return hostnames

    def _extract_k8s_ingress_hosts(
        self,
        attrs: dict,
    ) -> list[tuple[str, str]]:
        """Extract hosts from K8s Ingress spec."""
        hostnames = []

        spec = attrs.get("spec", {})
        rules = spec.get("rules", [])

        for rule in rules:
            host = rule.get("host")
            if host:
                hostnames.append((host, "hostname_match"))

        return hostnames

    def _extract_k8s_service_hosts(
        self,
        attrs: dict,
    ) -> list[tuple[str, str]]:
        """Extract external IPs/hostnames from K8s Service."""
        hostnames = []

        status = attrs.get("status", {})
        load_balancer = status.get("loadBalancer", {})
        ingress = load_balancer.get("ingress", [])

        for entry in ingress:
            hostname = entry.get("hostname")
            if hostname:
                hostnames.append((hostname, "hostname_match"))

            ip = entry.get("ip")
            if ip:
                hostnames.append((ip, "ip_match"))

        # Also check spec.externalIPs
        spec = attrs.get("spec", {})
        external_ips = spec.get("externalIPs", [])
        for ip in external_ips:
            hostnames.append((ip, "ip_match"))

        return hostnames

    def _extract_vmware_vm_hosts(
        self,
        attrs: dict,
    ) -> list[tuple[str, str]]:
        """Extract IP addresses and hostnames from VMware VM."""
        hostnames = []

        # Check guest info
        guest = attrs.get("guest", {})

        ip_address = guest.get("ip_address")
        if ip_address:
            hostnames.append((ip_address, "ip_match"))

        hostname = guest.get("hostname")
        if hostname:
            hostnames.append((hostname, "hostname_match"))

        # Also check runtime.host for host references
        # (though this is less useful for connector matching)

        return hostnames

    def _extract_gcp_instance_hosts(
        self,
        attrs: dict,
    ) -> list[tuple[str, str]]:
        """Extract IPs from GCP Instance."""
        hostnames = []

        # Check networkInterfaces
        network_interfaces = attrs.get("networkInterfaces", [])
        for iface in network_interfaces:
            # Internal IP
            network_ip = iface.get("networkIP")
            if network_ip:
                hostnames.append((network_ip, "ip_match"))

            # External IP (NAT)
            access_configs = iface.get("accessConfigs", [])
            for config in access_configs:
                nat_ip = config.get("natIP")
                if nat_ip:
                    hostnames.append((nat_ip, "ip_match"))

        return hostnames

    def _get_confidence_for_match_type(self, match_type: str) -> float:
        """Get confidence score for a match type."""
        if match_type == "hostname_match":  # noqa: SIM116 -- readability preferred over dict lookup
            return HOSTNAME_EXACT_MATCH_CONFIDENCE
        elif match_type == "ip_match":
            return IP_EXACT_MATCH_CONFIDENCE
        elif match_type == "partial_hostname":
            return PARTIAL_HOSTNAME_CONFIDENCE
        else:
            return 0.5  # Unknown match type

    def is_correlatable_entity(self, entity: TopologyEntityModel) -> bool:
        """
        Check if an entity should be checked for correlations.

        Only certain entity types can correlate with connector targets:
        - K8s Ingress (has external hostnames)
        - K8s Service with LoadBalancer (has external IPs)
        - VMware VM (has IPs/hostnames)
        - GCP Instance (has IPs)
        """
        if not entity.raw_attributes:
            return False

        attrs = entity.raw_attributes
        description = entity.description.lower() if entity.description else ""

        # K8s Ingress
        if attrs.get("kind") == "Ingress":
            return True

        # K8s Service with LoadBalancer
        if attrs.get("kind") == "Service":
            spec = attrs.get("spec", {})
            if spec.get("type") == "LoadBalancer":
                return True
            # Also check if it has externalIPs
            if spec.get("externalIPs"):
                return True

        # VMware VM
        if "vm" in description or "virtual machine" in description:
            guest = attrs.get("guest", {})
            if guest.get("ip_address") or guest.get("hostname"):
                return True

        # GCP Instance
        return attrs.get("kind") == "compute#instance"


def get_hostname_matcher(session: AsyncSession) -> HostnameMatcher:
    """Get a HostnameMatcher instance for dependency injection."""
    return HostnameMatcher(session)
