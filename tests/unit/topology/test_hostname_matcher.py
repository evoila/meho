# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for HostnameMatcher service.

Tests hostname/IP extraction and correlation detection.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from meho_app.modules.topology.hostname_matcher import (
    HOSTNAME_EXACT_MATCH_CONFIDENCE,
    IP_EXACT_MATCH_CONFIDENCE,
    HostnameMatcher,
)
from meho_app.modules.topology.models import TopologyEntityModel


class TestHostnameMatcherExtraction:
    """Tests for hostname extraction from entities."""

    @pytest.fixture
    def matcher(self):
        """Create HostnameMatcher with mocked session."""
        session = MagicMock()
        return HostnameMatcher(session)

    # =========================================================================
    # K8s Ingress extraction tests
    # =========================================================================

    def test_extract_k8s_ingress_single_host(self, matcher):
        """Test extracting single host from K8s Ingress."""
        entity = TopologyEntityModel(
            id=uuid4(),
            name="shop-ingress",
            description="K8s Ingress shop-ingress, ns default",
            raw_attributes={"kind": "Ingress", "spec": {"rules": [{"host": "shop.example.com"}]}},
            tenant_id="test-tenant",
        )

        hostnames = matcher._extract_hostnames_from_entity(entity)

        assert len(hostnames) == 1
        assert hostnames[0] == ("shop.example.com", "hostname_match")

    def test_extract_k8s_ingress_multiple_hosts(self, matcher):
        """Test extracting multiple hosts from K8s Ingress."""
        entity = TopologyEntityModel(
            id=uuid4(),
            name="multi-ingress",
            description="K8s Ingress multi-ingress",
            raw_attributes={
                "kind": "Ingress",
                "spec": {
                    "rules": [
                        {"host": "api.example.com"},
                        {"host": "web.example.com"},
                    ]
                },
            },
            tenant_id="test-tenant",
        )

        hostnames = matcher._extract_hostnames_from_entity(entity)

        assert len(hostnames) == 2
        assert ("api.example.com", "hostname_match") in hostnames
        assert ("web.example.com", "hostname_match") in hostnames

    def test_extract_k8s_ingress_no_hosts(self, matcher):
        """Test Ingress with no hosts returns empty list."""
        entity = TopologyEntityModel(
            id=uuid4(),
            name="empty-ingress",
            description="K8s Ingress empty-ingress",
            raw_attributes={"kind": "Ingress", "spec": {"rules": []}},
            tenant_id="test-tenant",
        )

        hostnames = matcher._extract_hostnames_from_entity(entity)

        assert len(hostnames) == 0

    # =========================================================================
    # K8s Service extraction tests
    # =========================================================================

    def test_extract_k8s_service_loadbalancer_ip(self, matcher):
        """Test extracting IP from K8s LoadBalancer Service."""
        entity = TopologyEntityModel(
            id=uuid4(),
            name="my-service",
            description="K8s Service my-service",
            raw_attributes={
                "kind": "Service",
                "spec": {"type": "LoadBalancer"},
                "status": {"loadBalancer": {"ingress": [{"ip": "34.123.45.67"}]}},
            },
            tenant_id="test-tenant",
        )

        hostnames = matcher._extract_hostnames_from_entity(entity)

        assert len(hostnames) == 1
        assert hostnames[0] == ("34.123.45.67", "ip_match")

    def test_extract_k8s_service_loadbalancer_hostname(self, matcher):
        """Test extracting hostname from AWS LoadBalancer Service."""
        entity = TopologyEntityModel(
            id=uuid4(),
            name="aws-service",
            description="K8s Service aws-service",
            raw_attributes={
                "kind": "Service",
                "spec": {"type": "LoadBalancer"},
                "status": {"loadBalancer": {"ingress": [{"hostname": "abc123.elb.amazonaws.com"}]}},
            },
            tenant_id="test-tenant",
        )

        hostnames = matcher._extract_hostnames_from_entity(entity)

        assert len(hostnames) == 1
        assert hostnames[0] == ("abc123.elb.amazonaws.com", "hostname_match")

    def test_extract_k8s_service_external_ips(self, matcher):
        """Test extracting external IPs from K8s Service."""
        entity = TopologyEntityModel(
            id=uuid4(),
            name="external-service",
            description="K8s Service external-service",
            raw_attributes={
                "kind": "Service",
                "spec": {"externalIPs": ["192.168.1.10", "192.168.1.11"]},
            },
            tenant_id="test-tenant",
        )

        hostnames = matcher._extract_hostnames_from_entity(entity)

        assert len(hostnames) == 2
        assert ("192.168.1.10", "ip_match") in hostnames
        assert ("192.168.1.11", "ip_match") in hostnames

    # =========================================================================
    # VMware VM extraction tests
    # =========================================================================

    def test_extract_vmware_vm_ip(self, matcher):
        """Test extracting IP from VMware VM."""
        entity = TopologyEntityModel(
            id=uuid4(),
            name="web-01",
            description="VMware VM web-01",
            raw_attributes={
                "guest": {
                    "ip_address": "10.0.0.50",
                }
            },
            tenant_id="test-tenant",
        )

        hostnames = matcher._extract_hostnames_from_entity(entity)

        assert len(hostnames) == 1
        assert hostnames[0] == ("10.0.0.50", "ip_match")

    def test_extract_vmware_vm_hostname(self, matcher):
        """Test extracting hostname from VMware VM."""
        entity = TopologyEntityModel(
            id=uuid4(),
            name="web-01",
            description="VMware VM web-01",
            raw_attributes={
                "guest": {
                    "hostname": "web-01.example.local",
                }
            },
            tenant_id="test-tenant",
        )

        hostnames = matcher._extract_hostnames_from_entity(entity)

        assert len(hostnames) == 1
        assert hostnames[0] == ("web-01.example.local", "hostname_match")

    def test_extract_vmware_vm_ip_and_hostname(self, matcher):
        """Test extracting both IP and hostname from VMware VM."""
        entity = TopologyEntityModel(
            id=uuid4(),
            name="web-01",
            description="VMware VM web-01",
            raw_attributes={
                "guest": {
                    "ip_address": "10.0.0.50",
                    "hostname": "web-01.example.local",
                }
            },
            tenant_id="test-tenant",
        )

        hostnames = matcher._extract_hostnames_from_entity(entity)

        assert len(hostnames) == 2
        assert ("10.0.0.50", "ip_match") in hostnames
        assert ("web-01.example.local", "hostname_match") in hostnames

    # =========================================================================
    # GCP Instance extraction tests
    # =========================================================================

    def test_extract_gcp_instance_nat_ip(self, matcher):
        """Test extracting NAT IP from GCP Instance."""
        entity = TopologyEntityModel(
            id=uuid4(),
            name="gce-instance-01",
            description="GCP Instance gce-instance-01",
            raw_attributes={
                "kind": "compute#instance",
                "networkInterfaces": [
                    {"networkIP": "10.128.0.5", "accessConfigs": [{"natIP": "35.222.111.222"}]}
                ],
            },
            tenant_id="test-tenant",
        )

        hostnames = matcher._extract_hostnames_from_entity(entity)

        assert len(hostnames) == 2
        assert ("10.128.0.5", "ip_match") in hostnames
        assert ("35.222.111.222", "ip_match") in hostnames

    # =========================================================================
    # Edge cases
    # =========================================================================

    def test_extract_empty_raw_attributes(self, matcher):
        """Test entity with no raw_attributes."""
        entity = TopologyEntityModel(
            id=uuid4(),
            name="empty-entity",
            description="Some entity",
            raw_attributes=None,
            tenant_id="test-tenant",
        )

        hostnames = matcher._extract_hostnames_from_entity(entity)

        assert len(hostnames) == 0

    def test_extract_unknown_entity_type(self, matcher):
        """Test entity with unknown type returns empty."""
        entity = TopologyEntityModel(
            id=uuid4(),
            name="unknown-entity",
            description="Some unknown entity type",
            raw_attributes={"foo": "bar"},
            tenant_id="test-tenant",
        )

        hostnames = matcher._extract_hostnames_from_entity(entity)

        assert len(hostnames) == 0


class TestHostnameMatcherIsCorrelatable:
    """Tests for is_correlatable_entity method."""

    @pytest.fixture
    def matcher(self):
        """Create HostnameMatcher with mocked session."""
        session = MagicMock()
        return HostnameMatcher(session)

    def test_k8s_ingress_is_correlatable(self, matcher):
        """Test K8s Ingress is correlatable."""
        entity = TopologyEntityModel(
            id=uuid4(),
            name="shop-ingress",
            description="K8s Ingress",
            raw_attributes={"kind": "Ingress"},
            tenant_id="test-tenant",
        )

        assert matcher.is_correlatable_entity(entity) is True

    def test_k8s_loadbalancer_service_is_correlatable(self, matcher):
        """Test K8s LoadBalancer Service is correlatable."""
        entity = TopologyEntityModel(
            id=uuid4(),
            name="my-service",
            description="K8s Service",
            raw_attributes={
                "kind": "Service",
                "spec": {"type": "LoadBalancer"},
            },
            tenant_id="test-tenant",
        )

        assert matcher.is_correlatable_entity(entity) is True

    def test_k8s_clusterip_service_not_correlatable(self, matcher):
        """Test K8s ClusterIP Service is NOT correlatable."""
        entity = TopologyEntityModel(
            id=uuid4(),
            name="internal-service",
            description="K8s Service",
            raw_attributes={
                "kind": "Service",
                "spec": {"type": "ClusterIP"},
            },
            tenant_id="test-tenant",
        )

        assert matcher.is_correlatable_entity(entity) is False

    def test_vmware_vm_with_ip_is_correlatable(self, matcher):
        """Test VMware VM with IP is correlatable."""
        entity = TopologyEntityModel(
            id=uuid4(),
            name="web-01",
            description="VMware VM web-01",
            raw_attributes={
                "guest": {"ip_address": "10.0.0.50"},
            },
            tenant_id="test-tenant",
        )

        assert matcher.is_correlatable_entity(entity) is True

    def test_vmware_vm_without_ip_not_correlatable(self, matcher):
        """Test VMware VM without IP is NOT correlatable."""
        entity = TopologyEntityModel(
            id=uuid4(),
            name="web-01",
            description="VMware VM web-01",
            raw_attributes={
                "guest": {},
            },
            tenant_id="test-tenant",
        )

        assert matcher.is_correlatable_entity(entity) is False

    def test_gcp_instance_is_correlatable(self, matcher):
        """Test GCP Instance is correlatable."""
        entity = TopologyEntityModel(
            id=uuid4(),
            name="gce-01",
            description="GCP Instance",
            raw_attributes={"kind": "compute#instance"},
            tenant_id="test-tenant",
        )

        assert matcher.is_correlatable_entity(entity) is True

    def test_entity_without_raw_attributes_not_correlatable(self, matcher):
        """Test entity without raw_attributes is not correlatable."""
        entity = TopologyEntityModel(
            id=uuid4(),
            name="empty",
            description="Empty entity",
            raw_attributes=None,
            tenant_id="test-tenant",
        )

        assert matcher.is_correlatable_entity(entity) is False


class TestHostnameMatcherConfidence:
    """Tests for confidence scoring."""

    @pytest.fixture
    def matcher(self):
        """Create HostnameMatcher with mocked session."""
        session = MagicMock()
        return HostnameMatcher(session)

    def test_hostname_match_confidence(self, matcher):
        """Test hostname match returns correct confidence."""
        confidence = matcher._get_confidence_for_match_type("hostname_match")
        assert confidence == HOSTNAME_EXACT_MATCH_CONFIDENCE

    def test_ip_match_confidence(self, matcher):
        """Test IP match returns correct confidence."""
        confidence = matcher._get_confidence_for_match_type("ip_match")
        assert confidence == IP_EXACT_MATCH_CONFIDENCE

    def test_unknown_match_type_confidence(self, matcher):
        """Test unknown match type returns default confidence."""
        confidence = matcher._get_confidence_for_match_type("unknown")
        assert confidence == pytest.approx(0.5)


class TestHostnameMatcherConfidenceRouting:
    """Tests for Phase 3 confidence-based routing in HostnameMatcher."""

    @pytest.fixture
    def matcher_with_mocks(self):
        """Create HostnameMatcher with mocked dependencies."""
        session = MagicMock()
        # Disable LLM verification for most tests
        return HostnameMatcher(session, enable_llm_verification=False)

    @pytest.fixture
    def matcher_with_llm(self):
        """Create HostnameMatcher with LLM verification enabled."""
        session = MagicMock()
        return HostnameMatcher(session, enable_llm_verification=True)

    @pytest.mark.asyncio
    async def test_high_confidence_auto_approves(self, matcher_with_mocks):
        """Test that high confidence suggestions are auto-approved."""
        from unittest.mock import patch

        suggestion = MagicMock()
        suggestion.id = uuid4()

        # Mock config with thresholds
        mock_config = MagicMock()
        mock_config.suggestion_auto_approve_threshold = 0.90
        mock_config.suggestion_llm_verify_threshold = 0.70

        with patch.object(matcher_with_mocks, "_config", mock_config):
            matcher_with_mocks.topology_repo = MagicMock()
            matcher_with_mocks.topology_repo.approve_suggestion = AsyncMock()

            # High confidence should trigger auto-approve
            await matcher_with_mocks._handle_suggestion_by_confidence(suggestion, 0.95)

            matcher_with_mocks.topology_repo.approve_suggestion.assert_called_once_with(
                suggestion_id=suggestion.id,
                user_id="auto_approve_high_confidence",
            )

    @pytest.mark.asyncio
    async def test_mid_confidence_triggers_llm_verification(self, matcher_with_llm):
        """Test that mid-confidence suggestions trigger LLM verification."""
        from unittest.mock import patch

        suggestion = MagicMock()
        suggestion.id = uuid4()

        mock_config = MagicMock()
        mock_config.suggestion_auto_approve_threshold = 0.90
        mock_config.suggestion_llm_verify_threshold = 0.70

        mock_verifier_instance = MagicMock()
        mock_verifier_instance.process_and_resolve = AsyncMock(return_value="approved")
        mock_verifier_class = MagicMock(return_value=mock_verifier_instance)

        with patch.object(matcher_with_llm, "_config", mock_config):  # noqa: SIM117 -- readability preferred over combined with
            # Patch at the module level where it's imported
            with patch.dict(
                "sys.modules",
                {
                    "meho_app.modules.topology.suggestion_verifier": MagicMock(
                        SuggestionVerifier=mock_verifier_class
                    )
                },
            ):
                await matcher_with_llm._handle_suggestion_by_confidence(suggestion, 0.85)

                mock_verifier_instance.process_and_resolve.assert_called_once_with(suggestion.id)

    @pytest.mark.asyncio
    async def test_low_confidence_stays_pending(self, matcher_with_mocks):
        """Test that low confidence suggestions stay pending (no action)."""
        from unittest.mock import patch

        suggestion = MagicMock()
        suggestion.id = uuid4()

        mock_config = MagicMock()
        mock_config.suggestion_auto_approve_threshold = 0.90
        mock_config.suggestion_llm_verify_threshold = 0.70

        with patch.object(matcher_with_mocks, "_config", mock_config):
            matcher_with_mocks.topology_repo = MagicMock()
            matcher_with_mocks.topology_repo.approve_suggestion = AsyncMock()

            # Low confidence should NOT trigger any action
            await matcher_with_mocks._handle_suggestion_by_confidence(suggestion, 0.65)

            matcher_with_mocks.topology_repo.approve_suggestion.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_verification_disabled(self, matcher_with_mocks):
        """Test that LLM verification is not triggered when disabled."""
        from unittest.mock import patch

        suggestion = MagicMock()
        suggestion.id = uuid4()

        mock_config = MagicMock()
        mock_config.suggestion_auto_approve_threshold = 0.90
        mock_config.suggestion_llm_verify_threshold = 0.70

        # matcher_with_mocks has enable_llm_verification=False
        assert matcher_with_mocks._enable_llm_verification is False

        with patch.object(matcher_with_mocks, "_config", mock_config):
            # Mid confidence with LLM disabled - should NOT call verifier
            # We just verify no exception is raised and method completes
            await matcher_with_mocks._handle_suggestion_by_confidence(suggestion, 0.85)

    @pytest.mark.asyncio
    async def test_llm_verification_failure_handled_gracefully(self, matcher_with_llm):
        """Test that LLM verification failures don't break the flow."""
        from unittest.mock import patch

        suggestion = MagicMock()
        suggestion.id = uuid4()

        mock_config = MagicMock()
        mock_config.suggestion_auto_approve_threshold = 0.90
        mock_config.suggestion_llm_verify_threshold = 0.70

        mock_verifier_instance = MagicMock()
        mock_verifier_instance.process_and_resolve = AsyncMock(side_effect=Exception("LLM error"))
        mock_verifier_class = MagicMock(return_value=mock_verifier_instance)

        with (
            patch.object(matcher_with_llm, "_config", mock_config),
            patch.dict(
                "sys.modules",
                {
                    "meho_app.modules.topology.suggestion_verifier": MagicMock(
                        SuggestionVerifier=mock_verifier_class
                    )
                },
            ),
        ):
            # Should not raise exception
            await matcher_with_llm._handle_suggestion_by_confidence(suggestion, 0.85)

            # Verifier was called but failed
            mock_verifier_instance.process_and_resolve.assert_called_once()
