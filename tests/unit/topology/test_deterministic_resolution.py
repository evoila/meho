# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for deterministic entity resolution engine.

TDD RED phase: Tests define expected behavior for:
- MatchEvidence and MatchPriority
- ProviderIDMatcher (GCE and vSphere formats)
- IPAddressMatcher (cross-connector IP comparison)
- HostnameMatcher (normalization and exact/partial distinction)
- DeterministicResolver (priority-ordered matcher chain)
"""

from uuid import uuid4

import pytest

from meho_app.modules.topology.models import TopologyEntityModel
from meho_app.modules.topology.resolution import get_default_resolver
from meho_app.modules.topology.resolution.evidence import (
    MatchEvidence,
    MatchPriority,
)
from meho_app.modules.topology.resolution.matchers.hostname import (
    HostnameMatcher as ResolutionHostnameMatcher,
)
from meho_app.modules.topology.resolution.matchers.ip_address import (
    IPAddressMatcher,
)
from meho_app.modules.topology.resolution.matchers.provider_id import (
    ProviderIDMatcher,
)
from meho_app.modules.topology.resolution.resolver import DeterministicResolver

# =============================================================================
# Helpers
# =============================================================================


def _make_entity(
    name: str,
    entity_type: str,
    connector_type: str,
    connector_id=None,
    raw_attributes=None,
    description="",
    tenant_id="test-tenant",
):
    """Create a TopologyEntityModel for testing."""
    return TopologyEntityModel(
        id=uuid4(),
        name=name,
        entity_type=entity_type,
        connector_type=connector_type,
        connector_id=connector_id or uuid4(),
        raw_attributes=raw_attributes or {},
        description=description or f"{connector_type} {entity_type} {name}",
        canonical_id=name,
        tenant_id=tenant_id,
    )


# =============================================================================
# MatchEvidence and MatchPriority
# =============================================================================


class TestMatchEvidence:
    """Tests for MatchEvidence dataclass and MatchPriority enum."""

    def test_match_evidence_creation(self):
        """MatchEvidence dataclass stores match_type, matched_values, confidence, auto_confirm."""
        evidence = MatchEvidence(
            match_type="provider_id",
            matched_values={"provider_id": "gce://proj/zone/vm", "vm_name": "vm"},
            confidence=1.0,
            auto_confirm=True,
        )
        assert evidence.match_type == "provider_id"
        assert evidence.matched_values == {"provider_id": "gce://proj/zone/vm", "vm_name": "vm"}
        assert evidence.confidence == pytest.approx(1.0)
        assert evidence.auto_confirm is True

    def test_match_priority_ordering(self):
        """MatchPriority has correct priority order: PROVIDER_ID < IP_ADDRESS < HOSTNAME."""
        assert MatchPriority.PROVIDER_ID < MatchPriority.IP_ADDRESS
        assert MatchPriority.IP_ADDRESS < MatchPriority.HOSTNAME
        assert MatchPriority.PROVIDER_ID.value == 1
        assert MatchPriority.IP_ADDRESS.value == 2
        assert MatchPriority.HOSTNAME.value == 3


# =============================================================================
# ProviderIDMatcher
# =============================================================================


class TestProviderIDMatcher:
    """Tests for ProviderIDMatcher with GCE and vSphere format parsing."""

    @pytest.fixture
    def matcher(self):
        return ProviderIDMatcher()

    def test_gce_provider_id_match(self, matcher):
        """K8s Node with GCE providerID matches GCP Instance by vm-name."""
        k8s_node = _make_entity(
            name="gke-node-abc",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "_extracted_provider_id": "gce://my-project/us-central1-a/gke-node-abc",
            },
        )
        gcp_instance = _make_entity(
            name="gke-node-abc",
            entity_type="Instance",
            connector_type="gcp",
        )

        evidence = matcher.match(k8s_node, gcp_instance)

        assert evidence is not None
        assert evidence.match_type == "provider_id"
        assert evidence.confidence == pytest.approx(1.0)
        assert evidence.auto_confirm is True
        assert "gce://" in evidence.matched_values["provider_id"]
        assert evidence.matched_values["vm_name"] == "gke-node-abc"

    def test_vsphere_provider_id_match(self, matcher):
        """K8s Node with vSphere providerID matches VMware VM by moref."""
        k8s_node = _make_entity(
            name="worker-01",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "_extracted_provider_id": "vsphere://dc1/vm/vm-1234",
            },
        )
        vmware_vm = _make_entity(
            name="worker-01",
            entity_type="VM",
            connector_type="vmware",
            raw_attributes={
                "_extracted_moref": "vm-1234",
            },
        )

        evidence = matcher.match(k8s_node, vmware_vm)

        assert evidence is not None
        assert evidence.match_type == "provider_id"
        assert evidence.confidence == pytest.approx(1.0)
        assert evidence.auto_confirm is True

    def test_no_provider_id_returns_none(self, matcher):
        """K8s Node without _extracted_provider_id returns None."""
        k8s_node = _make_entity(
            name="bare-node",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={},
        )
        gcp_instance = _make_entity(
            name="some-instance",
            entity_type="Instance",
            connector_type="gcp",
        )

        evidence = matcher.match(k8s_node, gcp_instance)
        assert evidence is None

    def test_fallback_to_spec_provider_id(self, matcher):
        """K8s Node with providerID in nested spec.providerID (fallback) still finds it."""
        k8s_node = _make_entity(
            name="gke-node-xyz",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "spec": {
                    "providerID": "gce://my-project/us-east1-b/gke-node-xyz",
                },
            },
        )
        gcp_instance = _make_entity(
            name="gke-node-xyz",
            entity_type="Instance",
            connector_type="gcp",
        )

        evidence = matcher.match(k8s_node, gcp_instance)

        assert evidence is not None
        assert evidence.match_type == "provider_id"
        assert evidence.matched_values["vm_name"] == "gke-node-xyz"

    def test_malformed_provider_id_returns_none(self, matcher):
        """Malformed providerID 'gce://bad' returns None."""
        k8s_node = _make_entity(
            name="bad-node",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "_extracted_provider_id": "gce://bad",
            },
        )
        gcp_instance = _make_entity(
            name="some-instance",
            entity_type="Instance",
            connector_type="gcp",
        )

        evidence = matcher.match(k8s_node, gcp_instance)
        assert evidence is None

    def test_gce_provider_id_name_mismatch(self, matcher):
        """GCE providerID with mismatched vm-name returns None."""
        k8s_node = _make_entity(
            name="node-a",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "_extracted_provider_id": "gce://proj/zone/node-a",
            },
        )
        gcp_instance = _make_entity(
            name="different-instance",
            entity_type="Instance",
            connector_type="gcp",
        )

        evidence = matcher.match(k8s_node, gcp_instance)
        assert evidence is None

    def test_vsphere_provider_id_moref_mismatch(self, matcher):
        """vSphere providerID with mismatched moref returns None."""
        k8s_node = _make_entity(
            name="worker-01",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "_extracted_provider_id": "vsphere://dc1/vm/vm-1234",
            },
        )
        vmware_vm = _make_entity(
            name="worker-01",
            entity_type="VM",
            connector_type="vmware",
            raw_attributes={
                "_extracted_moref": "vm-5678",
            },
        )

        evidence = matcher.match(k8s_node, vmware_vm)
        assert evidence is None

    def test_empty_raw_attributes(self, matcher):
        """Entity with empty raw_attributes returns None gracefully."""
        entity_a = _make_entity(
            name="node-a",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={},
        )
        entity_b = _make_entity(
            name="vm-b",
            entity_type="VM",
            connector_type="vmware",
            raw_attributes={},
        )

        evidence = matcher.match(entity_a, entity_b)
        assert evidence is None

    def test_none_raw_attributes(self, matcher):
        """Entity with None raw_attributes returns None gracefully."""
        entity_a = _make_entity(
            name="node-a",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes=None,
        )
        entity_b = _make_entity(
            name="vm-b",
            entity_type="VM",
            connector_type="vmware",
        )

        evidence = matcher.match(entity_a, entity_b)
        assert evidence is None

    # --- AWS EKS providerID tests ---

    def test_aws_provider_id_match(self, matcher):
        """AWS providerID matches cloud entity by instance_id attribute."""
        k8s_node = _make_entity(
            name="eks-node-abc",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "_extracted_provider_id": "aws:///eu-west-1a/i-0cb3f1ceeb038fb6c",
            },
        )
        aws_instance = _make_entity(
            name="eks-node-abc",
            entity_type="Instance",
            connector_type="aws",
            raw_attributes={
                "_extracted_instance_id": "i-0cb3f1ceeb038fb6c",
            },
        )

        evidence = matcher.match(k8s_node, aws_instance)

        assert evidence is not None
        assert evidence.match_type == "provider_id"
        assert evidence.confidence == pytest.approx(1.0)
        assert evidence.auto_confirm is True
        assert evidence.matched_values["instance_id"] == "i-0cb3f1ceeb038fb6c"

    def test_aws_provider_id_match_by_name(self, matcher):
        """AWS providerID matches cloud entity by name when no instance_id attribute."""
        k8s_node = _make_entity(
            name="eks-node-abc",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "_extracted_provider_id": "aws:///us-east-1b/i-02e80d4889b6ccffa",
            },
        )
        aws_instance = _make_entity(
            name="i-02e80d4889b6ccffa",
            entity_type="Instance",
            connector_type="aws",
        )

        evidence = matcher.match(k8s_node, aws_instance)

        assert evidence is not None
        assert evidence.match_type == "provider_id"
        assert evidence.confidence == pytest.approx(1.0)
        assert evidence.matched_values["instance_id"] == "i-02e80d4889b6ccffa"

    def test_aws_provider_id_malformed(self, matcher):
        """Malformed AWS providerID 'aws:///bad' returns None."""
        k8s_node = _make_entity(
            name="bad-node",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "_extracted_provider_id": "aws:///bad",
            },
        )
        aws_instance = _make_entity(
            name="some-instance",
            entity_type="Instance",
            connector_type="aws",
        )

        evidence = matcher.match(k8s_node, aws_instance)
        assert evidence is None

    # --- Azure AKS providerID tests ---

    def test_azure_provider_id_standalone_vm_match(self, matcher):
        """Azure standalone VM providerID matches cloud entity by instance_id attribute."""
        k8s_node = _make_entity(
            name="aks-node-01",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "_extracted_provider_id": (
                    "azure:///subscriptions/sub-1/resourceGroups/rg-1"
                    "/providers/Microsoft.Compute/virtualMachines/my-vm"
                ),
            },
        )
        azure_vm = _make_entity(
            name="my-vm",
            entity_type="VM",
            connector_type="azure",
            raw_attributes={
                "_extracted_instance_id": "my-vm",
            },
        )

        evidence = matcher.match(k8s_node, azure_vm)

        assert evidence is not None
        assert evidence.match_type == "provider_id"
        assert evidence.confidence == pytest.approx(1.0)
        assert evidence.auto_confirm is True
        assert evidence.matched_values["vm_identifier"] == "my-vm"

    def test_azure_provider_id_vmss_match(self, matcher):
        """Azure VMSS providerID matches cloud entity by vmss-name_instance-id identifier."""
        k8s_node = _make_entity(
            name="aks-vmss-node",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "_extracted_provider_id": (
                    "azure:///subscriptions/sub-1/resourceGroups/rg-1"
                    "/providers/Microsoft.Compute/virtualMachineScaleSets/vmss-1/virtualMachines/0"
                ),
            },
        )
        azure_vmss_instance = _make_entity(
            name="vmss-1_0",
            entity_type="VM",
            connector_type="azure",
            raw_attributes={
                "_extracted_instance_id": "vmss-1_0",
            },
        )

        evidence = matcher.match(k8s_node, azure_vmss_instance)

        assert evidence is not None
        assert evidence.match_type == "provider_id"
        assert evidence.confidence == pytest.approx(1.0)
        assert evidence.auto_confirm is True
        assert evidence.matched_values["vm_identifier"] == "vmss-1_0"

    def test_azure_provider_id_case_insensitive(self, matcher):
        """Mixed-case Azure providerID matches (case-insensitive regex)."""
        k8s_node = _make_entity(
            name="aks-node-ci",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "_extracted_provider_id": (
                    "azure:///subscriptions/sub-1/RESOURCEGROUPS/rg-1"
                    "/PROVIDERS/Microsoft.Compute/virtualMachines/ci-vm"
                ),
            },
        )
        azure_vm = _make_entity(
            name="ci-vm",
            entity_type="VM",
            connector_type="azure",
            raw_attributes={
                "_extracted_instance_id": "ci-vm",
            },
        )

        evidence = matcher.match(k8s_node, azure_vm)

        assert evidence is not None
        assert evidence.match_type == "provider_id"
        assert evidence.confidence == pytest.approx(1.0)
        assert evidence.matched_values["vm_identifier"] == "ci-vm"


# =============================================================================
# IPAddressMatcher
# =============================================================================


class TestIPAddressMatcher:
    """Tests for IPAddressMatcher with cross-connector IP comparison."""

    @pytest.fixture
    def matcher(self):
        return IPAddressMatcher()

    def test_k8s_node_vmware_vm_internal_ip_match(self, matcher):
        """K8s Node InternalIP matches VMware VM _extracted_ip_address."""
        k8s_node = _make_entity(
            name="node-01",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "_extracted_addresses": [
                    {"type": "InternalIP", "address": "10.128.0.42"},
                ],
            },
        )
        vmware_vm = _make_entity(
            name="vm-01",
            entity_type="VM",
            connector_type="vmware",
            raw_attributes={
                "_extracted_ip_address": "10.128.0.42",
            },
        )

        evidence = matcher.match(k8s_node, vmware_vm)

        assert evidence is not None
        assert evidence.match_type == "ip_address"
        assert evidence.confidence == pytest.approx(1.0)
        assert evidence.auto_confirm is True

    def test_k8s_node_gcp_instance_external_ip_match(self, matcher):
        """K8s Node ExternalIP matches GCP Instance natIP."""
        k8s_node = _make_entity(
            name="node-01",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "_extracted_addresses": [
                    {"type": "ExternalIP", "address": "35.222.111.222"},
                ],
            },
        )
        gcp_instance = _make_entity(
            name="gke-node-01",
            entity_type="Instance",
            connector_type="gcp",
            raw_attributes={
                "_extracted_network_interfaces": [
                    {
                        "networkIP": "10.128.0.42",
                        "accessConfigs": [{"natIP": "35.222.111.222"}],
                    },
                ],
            },
        )

        evidence = matcher.match(k8s_node, gcp_instance)

        assert evidence is not None
        assert evidence.match_type == "ip_address"
        assert evidence.confidence == pytest.approx(1.0)
        assert evidence.auto_confirm is True

    def test_no_overlapping_ips_returns_none(self, matcher):
        """Entities with no overlapping IPs returns None."""
        k8s_node = _make_entity(
            name="node-01",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "_extracted_addresses": [
                    {"type": "InternalIP", "address": "10.128.0.42"},
                ],
            },
        )
        vmware_vm = _make_entity(
            name="vm-01",
            entity_type="VM",
            connector_type="vmware",
            raw_attributes={
                "_extracted_ip_address": "192.168.1.100",
            },
        )

        evidence = matcher.match(k8s_node, vmware_vm)
        assert evidence is None

    def test_invalid_ip_strings_silently_skipped(self, matcher):
        """Invalid IP strings are silently skipped (no crash)."""
        k8s_node = _make_entity(
            name="node-01",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "_extracted_addresses": [
                    {"type": "InternalIP", "address": "not-an-ip"},
                    {"type": "InternalIP", "address": "10.128.0.42"},
                ],
            },
        )
        vmware_vm = _make_entity(
            name="vm-01",
            entity_type="VM",
            connector_type="vmware",
            raw_attributes={
                "_extracted_ip_address": "10.128.0.42",
            },
        )

        evidence = matcher.match(k8s_node, vmware_vm)

        assert evidence is not None
        assert evidence.match_type == "ip_address"

    def test_gcp_network_ip_match(self, matcher):
        """GCP Instance networkIP matches VMware VM IP."""
        gcp_instance = _make_entity(
            name="gke-node-01",
            entity_type="Instance",
            connector_type="gcp",
            raw_attributes={
                "_extracted_network_interfaces": [
                    {"networkIP": "10.128.0.42"},
                ],
            },
        )
        vmware_vm = _make_entity(
            name="vm-01",
            entity_type="VM",
            connector_type="vmware",
            raw_attributes={
                "_extracted_ip_address": "10.128.0.42",
            },
        )

        evidence = matcher.match(gcp_instance, vmware_vm)

        assert evidence is not None
        assert evidence.match_type == "ip_address"

    def test_empty_addresses_returns_none(self, matcher):
        """Entity with empty addresses array returns None."""
        k8s_node = _make_entity(
            name="node-01",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "_extracted_addresses": [],
            },
        )
        vmware_vm = _make_entity(
            name="vm-01",
            entity_type="VM",
            connector_type="vmware",
            raw_attributes={},
        )

        evidence = matcher.match(k8s_node, vmware_vm)
        assert evidence is None

    def test_ip_from_rest_connector_base_url(self, matcher):
        """IP extracted from REST connector base_url matches VMware VM."""
        rest_entity = _make_entity(
            name="my-api-connector",
            entity_type="Connector",
            connector_type="rest",
            raw_attributes={
                "base_url": "http://192.168.1.10:8080/api",
            },
        )
        vmware_vm = _make_entity(
            name="web-server",
            entity_type="VM",
            connector_type="vmware",
            raw_attributes={
                "_extracted_ip_address": "192.168.1.10",
            },
        )

        evidence = matcher.match(rest_entity, vmware_vm)

        assert evidence is not None
        assert evidence.match_type == "ip_address"

    def test_no_raw_attributes_returns_none(self, matcher):
        """Entity with no raw_attributes returns None gracefully."""
        entity_a = _make_entity(
            name="node-a",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes=None,
        )
        entity_b = _make_entity(
            name="vm-b",
            entity_type="VM",
            connector_type="vmware",
            raw_attributes=None,
        )

        evidence = matcher.match(entity_a, entity_b)
        assert evidence is None

    def test_k8s_node_nested_status_addresses(self, matcher):
        """K8s Node with addresses in nested status.addresses also works."""
        k8s_node = _make_entity(
            name="node-01",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "status": {
                    "addresses": [
                        {"type": "InternalIP", "address": "10.128.0.42"},
                    ],
                },
            },
        )
        vmware_vm = _make_entity(
            name="vm-01",
            entity_type="VM",
            connector_type="vmware",
            raw_attributes={
                "_extracted_ip_address": "10.128.0.42",
            },
        )

        evidence = matcher.match(k8s_node, vmware_vm)

        assert evidence is not None
        assert evidence.match_type == "ip_address"


# =============================================================================
# HostnameMatcher (Resolution)
# =============================================================================


class TestResolutionHostnameMatcher:
    """Tests for the resolution HostnameMatcher with normalization."""

    @pytest.fixture
    def matcher(self):
        return ResolutionHostnameMatcher()

    def test_fqdn_to_short_name_match(self, matcher):
        """FQDN hostname normalizes to match short hostname."""
        entity_a = _make_entity(
            name="gke-node-abc.us-central1-a.c.myproject.internal",
            entity_type="Node",
            connector_type="kubernetes",
        )
        entity_b = _make_entity(
            name="gke-node-abc",
            entity_type="Instance",
            connector_type="gcp",
        )

        evidence = matcher.match(entity_a, entity_b)

        assert evidence is not None
        assert evidence.match_type == "hostname_exact"
        assert evidence.confidence == pytest.approx(0.95)
        assert evidence.auto_confirm is True

    def test_dot_internal_suffix_stripped(self, matcher):
        """Hostname with .internal suffix normalizes to match."""
        entity_a = _make_entity(
            name="worker-01.internal",
            entity_type="VM",
            connector_type="vmware",
        )
        entity_b = _make_entity(
            name="worker-01.local",
            entity_type="Node",
            connector_type="kubernetes",
        )

        evidence = matcher.match(entity_a, entity_b)

        assert evidence is not None
        assert evidence.match_type == "hostname_exact"
        assert evidence.auto_confirm is True

    def test_exact_name_match(self, matcher):
        """Identical entity names match exactly."""
        entity_a = _make_entity(
            name="node-abc",
            entity_type="Node",
            connector_type="kubernetes",
        )
        entity_b = _make_entity(
            name="node-abc",
            entity_type="VM",
            connector_type="vmware",
        )

        evidence = matcher.match(entity_a, entity_b)

        assert evidence is not None
        assert evidence.match_type == "hostname_exact"
        assert evidence.confidence == pytest.approx(0.95)
        assert evidence.auto_confirm is True

    def test_case_insensitive_comparison(self, matcher):
        """Hostname comparison is case-insensitive."""
        entity_a = _make_entity(
            name="Worker-01",
            entity_type="Node",
            connector_type="kubernetes",
        )
        entity_b = _make_entity(
            name="worker-01",
            entity_type="VM",
            connector_type="vmware",
        )

        evidence = matcher.match(entity_a, entity_b)

        assert evidence is not None
        assert evidence.match_type == "hostname_exact"

    def test_no_hostname_match_returns_none(self, matcher):
        """Different hostnames return None."""
        entity_a = _make_entity(
            name="node-abc",
            entity_type="Node",
            connector_type="kubernetes",
        )
        entity_b = _make_entity(
            name="totally-different",
            entity_type="VM",
            connector_type="vmware",
        )

        evidence = matcher.match(entity_a, entity_b)
        assert evidence is None

    def test_extracted_hostname_from_raw_attributes(self, matcher):
        """Matcher checks raw_attributes for _extracted_hostname."""
        entity_a = _make_entity(
            name="vm-1234",
            entity_type="VM",
            connector_type="vmware",
            raw_attributes={
                "_extracted_hostname": "worker-node-01",
            },
        )
        entity_b = _make_entity(
            name="worker-node-01",
            entity_type="Node",
            connector_type="kubernetes",
        )

        evidence = matcher.match(entity_a, entity_b)

        assert evidence is not None
        assert evidence.match_type == "hostname_exact"

    def test_guest_hostname_from_raw_attributes(self, matcher):
        """Matcher checks raw_attributes for guest.hostname."""
        entity_a = _make_entity(
            name="vm-5678",
            entity_type="VM",
            connector_type="vmware",
            raw_attributes={
                "guest": {"hostname": "k8s-worker-02"},
            },
        )
        entity_b = _make_entity(
            name="k8s-worker-02",
            entity_type="Node",
            connector_type="kubernetes",
        )

        evidence = matcher.match(entity_a, entity_b)

        assert evidence is not None
        assert evidence.match_type == "hostname_exact"

    def test_rest_connector_base_url_hostname_match(self, matcher):
        """REST connector entity base_url hostname matches K8s Ingress name."""
        rest_entity = _make_entity(
            name="my-api-connector",
            entity_type="Connector",
            connector_type="rest",
            raw_attributes={
                "base_url": "https://api.myapp.com:8443/v2",
            },
        )
        k8s_ingress = _make_entity(
            name="api.myapp.com",
            entity_type="Ingress",
            connector_type="kubernetes",
        )

        evidence = matcher.match(rest_entity, k8s_ingress)

        assert evidence is not None
        assert evidence.match_type == "hostname_exact"
        assert evidence.confidence == pytest.approx(0.95)
        assert evidence.auto_confirm is True

    def test_compute_googleapis_suffix_stripped(self, matcher):
        """Hostname with .compute.googleapis.com suffix is stripped."""
        entity_a = _make_entity(
            name="node-abc.compute.googleapis.com",
            entity_type="Instance",
            connector_type="gcp",
        )
        entity_b = _make_entity(
            name="node-abc",
            entity_type="Node",
            connector_type="kubernetes",
        )

        evidence = matcher.match(entity_a, entity_b)

        assert evidence is not None
        assert evidence.match_type == "hostname_exact"

    def test_localdomain_suffix_stripped(self, matcher):
        """Hostname with .localdomain suffix is stripped."""
        entity_a = _make_entity(
            name="server-01.localdomain",
            entity_type="VM",
            connector_type="vmware",
        )
        entity_b = _make_entity(
            name="server-01",
            entity_type="Node",
            connector_type="kubernetes",
        )

        evidence = matcher.match(entity_a, entity_b)

        assert evidence is not None
        assert evidence.match_type == "hostname_exact"

    def test_k8s_ingress_spec_rules_host(self, matcher):
        """Matcher extracts hostname from K8s Ingress spec.rules[].host."""
        k8s_ingress = _make_entity(
            name="shop-ingress",
            entity_type="Ingress",
            connector_type="kubernetes",
            raw_attributes={
                "kind": "Ingress",
                "spec": {
                    "rules": [
                        {"host": "shop.example.com"},
                    ],
                },
            },
        )
        rest_entity = _make_entity(
            name="shop-api",
            entity_type="Connector",
            connector_type="rest",
            raw_attributes={
                "base_url": "https://shop.example.com/api",
            },
        )

        evidence = matcher.match(k8s_ingress, rest_entity)

        assert evidence is not None
        assert evidence.match_type == "hostname_exact"


# =============================================================================
# DeterministicResolver
# =============================================================================


class TestDeterministicResolver:
    """Tests for DeterministicResolver with priority-ordered matcher chain."""

    @pytest.fixture
    def resolver(self):
        """Create a resolver with all three matchers."""
        return DeterministicResolver(
            matchers=[
                ProviderIDMatcher(),
                IPAddressMatcher(),
                ResolutionHostnameMatcher(),
            ]
        )

    def test_priority_order_provider_id_first(self, resolver):
        """Resolver tries providerID matcher first."""
        k8s_node = _make_entity(
            name="gke-node-abc",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "_extracted_provider_id": "gce://proj/zone/gke-node-abc",
                "_extracted_addresses": [
                    {"type": "InternalIP", "address": "10.128.0.42"},
                ],
            },
        )
        gcp_instance = _make_entity(
            name="gke-node-abc",
            entity_type="Instance",
            connector_type="gcp",
            raw_attributes={
                "_extracted_network_interfaces": [
                    {"networkIP": "10.128.0.42"},
                ],
            },
        )

        evidence = resolver.resolve_pair(k8s_node, gcp_instance)

        assert evidence is not None
        # Should be provider_id match, not ip_address, because providerID has higher priority
        assert evidence.match_type == "provider_id"

    def test_short_circuit_on_first_match(self, resolver):
        """Resolver returns first match found, not all matches."""
        k8s_node = _make_entity(
            name="node-01",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes={
                "_extracted_addresses": [
                    {"type": "InternalIP", "address": "10.128.0.42"},
                ],
            },
        )
        vmware_vm = _make_entity(
            name="node-01",
            entity_type="VM",
            connector_type="vmware",
            raw_attributes={
                "_extracted_ip_address": "10.128.0.42",
            },
        )

        evidence = resolver.resolve_pair(k8s_node, vmware_vm)

        assert evidence is not None
        # IP match comes before hostname match
        assert evidence.match_type == "ip_address"

    def test_no_match_returns_none(self, resolver):
        """If no matcher produces a match, returns None."""
        entity_a = _make_entity(
            name="entity-a",
            entity_type="Node",
            connector_type="kubernetes",
        )
        entity_b = _make_entity(
            name="completely-different",
            entity_type="VM",
            connector_type="vmware",
        )

        evidence = resolver.resolve_pair(entity_a, entity_b)
        assert evidence is None

    def test_same_connector_skipped(self, resolver):
        """Entities from the same connector are never matched."""
        shared_connector_id = uuid4()
        entity_a = _make_entity(
            name="node-a",
            entity_type="Node",
            connector_type="kubernetes",
            connector_id=shared_connector_id,
        )
        entity_b = _make_entity(
            name="node-a",
            entity_type="Node",
            connector_type="kubernetes",
            connector_id=shared_connector_id,
        )

        evidence = resolver.resolve_pair(entity_a, entity_b)
        assert evidence is None

    def test_same_as_eligibility_check(self, resolver):
        """Incompatible entity types return None immediately."""
        # Pod has no same_as eligibility (same_as=None in schema)
        k8s_pod = _make_entity(
            name="nginx-pod",
            entity_type="Pod",
            connector_type="kubernetes",
        )
        vmware_vm = _make_entity(
            name="nginx-pod",
            entity_type="VM",
            connector_type="vmware",
        )

        evidence = resolver.resolve_pair(k8s_pod, vmware_vm)
        assert evidence is None

    def test_resolve_batch(self, resolver):
        """resolve_batch compares entity lists and returns match triples."""
        connector_a_id = uuid4()
        connector_b_id = uuid4()

        k8s_node = _make_entity(
            name="node-01",
            entity_type="Node",
            connector_type="kubernetes",
            connector_id=connector_a_id,
            raw_attributes={
                "_extracted_addresses": [
                    {"type": "InternalIP", "address": "10.128.0.42"},
                ],
            },
        )
        vmware_vm_match = _make_entity(
            name="worker-01",
            entity_type="VM",
            connector_type="vmware",
            connector_id=connector_b_id,
            raw_attributes={
                "_extracted_ip_address": "10.128.0.42",
            },
        )
        vmware_vm_no_match = _make_entity(
            name="other-vm",
            entity_type="VM",
            connector_type="vmware",
            connector_id=connector_b_id,
            raw_attributes={
                "_extracted_ip_address": "192.168.1.100",
            },
        )

        results = resolver.resolve_batch(
            entities_a=[k8s_node],
            entities_b=[vmware_vm_match, vmware_vm_no_match],
        )

        assert len(results) == 1
        entity_a, entity_b, evidence = results[0]
        assert entity_a.name == "node-01"
        assert entity_b.name == "worker-01"
        assert evidence.match_type == "ip_address"

    def test_resolve_batch_empty_lists(self, resolver):
        """resolve_batch with empty lists returns empty results."""
        results = resolver.resolve_batch(entities_a=[], entities_b=[])
        assert results == []

    def test_fallback_to_hostname(self, resolver):
        """When no providerID or IP match, falls back to hostname."""
        k8s_node = _make_entity(
            name="worker-node-01",
            entity_type="Node",
            connector_type="kubernetes",
        )
        vmware_vm = _make_entity(
            name="worker-node-01",
            entity_type="VM",
            connector_type="vmware",
        )

        evidence = resolver.resolve_pair(k8s_node, vmware_vm)

        assert evidence is not None
        assert evidence.match_type == "hostname_exact"

    def test_entity_no_raw_attributes(self, resolver):
        """Entity with no raw_attributes is handled gracefully."""
        entity_a = _make_entity(
            name="node-a",
            entity_type="Node",
            connector_type="kubernetes",
            raw_attributes=None,
        )
        entity_b = _make_entity(
            name="different-name",
            entity_type="VM",
            connector_type="vmware",
            raw_attributes=None,
        )

        evidence = resolver.resolve_pair(entity_a, entity_b)
        assert evidence is None


# =============================================================================
# Module Exports
# =============================================================================


class TestModuleExports:
    """Tests for public module exports."""

    def test_get_default_resolver(self):
        """get_default_resolver returns a configured DeterministicResolver."""
        resolver = get_default_resolver()
        assert isinstance(resolver, DeterministicResolver)

    def test_default_resolver_has_all_matchers(self):
        """Default resolver has all three matchers."""
        resolver = get_default_resolver()
        assert len(resolver.matchers) == 3
