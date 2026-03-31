# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ProviderID matcher for deterministic entity resolution.

Parses Kubernetes providerID values to match K8s Nodes against cloud VMs:
- GCE format: gce://project/zone/vm-name -> matches GCP Instance by name
- vSphere format: vsphere://datacenter/vm/vm-moref -> matches VMware VM by moref
- AWS EKS format: aws:///availability-zone/instance-id -> matches EC2 Instance by instance_id
- Azure AKS format: azure:///subscriptions/.../virtualMachines/name -> matches Azure VM by identifier
  (supports both standalone VMs and VMSS instances; case-insensitive)
"""

import re

from meho_app.modules.topology.models import TopologyEntityModel
from meho_app.modules.topology.resolution.evidence import MatchEvidence, MatchPriority
from meho_app.modules.topology.resolution.matchers.base import BaseMatcher

# GCE format: gce://project/zone/vm-name
_GCE_PATTERN = re.compile(r"^gce://([^/]+)/([^/]+)/(.+)$")

# vSphere format: vsphere://datacenter/vm/vm-identifier
_VSPHERE_PATTERN = re.compile(r"^vsphere://([^/]+)/vm/(.+)$")

# AWS EKS format: aws:///availability-zone/instance-id
_AWS_PATTERN = re.compile(r"^aws:///([a-z0-9-]+)/(.+)$")

# Azure AKS format: azure:///subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Compute/...
# Supports both standalone VMs and VMSS instances.
# IMPORTANT: re.IGNORECASE due to documented case inconsistency in Azure providerIDs
# (see kubernetes/kubernetes#71994)
_AZURE_PATTERN = re.compile(
    r"^azure:///subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/Microsoft\.Compute/"
    r"(?:virtualMachineScaleSets/([^/]+)/virtualMachines/(\d+)|virtualMachines/(.+))$",
    re.IGNORECASE,
)


class ProviderIDMatcher(BaseMatcher):
    """
    Matches K8s Nodes to cloud VMs via providerID parsing.

    Extracts providerID from K8s Node raw_attributes (checking both
    _extracted_provider_id and nested spec.providerID), then parses
    the provider-specific format to extract the VM identifier.

    For GCE: extracts vm-name and matches against GCP Instance entity name.
    For vSphere: extracts vm-moref and matches against VMware VM _extracted_moref.
    For AWS EKS: extracts instance-id and matches against EC2 instance_id or entity name.
    For Azure AKS: extracts vm_identifier and matches against Azure VM instance_id or name.
    """

    priority = MatchPriority.PROVIDER_ID

    def match(
        self,
        entity_a: TopologyEntityModel,
        entity_b: TopologyEntityModel,
    ) -> MatchEvidence | None:
        """Try to match via providerID in either direction."""
        # Try entity_a as K8s node, entity_b as cloud VM
        result = self._try_match(entity_a, entity_b)
        if result:
            return result

        # Try entity_b as K8s node, entity_a as cloud VM
        return self._try_match(entity_b, entity_a)

    def _try_match(
        self,
        k8s_entity: TopologyEntityModel,
        cloud_entity: TopologyEntityModel,
    ) -> MatchEvidence | None:
        """Try matching k8s_entity's providerID against cloud_entity."""
        provider_id = self._extract_provider_id(k8s_entity)
        if not provider_id:
            return None

        # Try GCE format
        gce_result = self._parse_gce(provider_id)
        if gce_result:
            _project, _zone, vm_name = gce_result
            if vm_name == cloud_entity.name:
                return MatchEvidence(
                    match_type="provider_id",
                    matched_values={
                        "provider_id": provider_id,
                        "vm_name": vm_name,
                    },
                    confidence=1.0,
                    auto_confirm=True,
                )
            return None

        # Try vSphere format
        vsphere_result = self._parse_vsphere(provider_id)
        if vsphere_result:
            _datacenter, vm_id = vsphere_result
            # Match against VMware VM moref
            cloud_attrs = cloud_entity.raw_attributes or {}
            cloud_moref = cloud_attrs.get("_extracted_moref") or cloud_attrs.get("moref")
            if cloud_moref and vm_id == cloud_moref:
                return MatchEvidence(
                    match_type="provider_id",
                    matched_values={
                        "provider_id": provider_id,
                        "vm_id": vm_id,
                    },
                    confidence=1.0,
                    auto_confirm=True,
                )
            return None

        # Try AWS EKS format
        aws_result = self._parse_aws(provider_id)
        if aws_result:
            _az, instance_id = aws_result
            # Match against AWS EC2 instance ID in cloud entity attributes
            cloud_attrs = cloud_entity.raw_attributes or {}
            cloud_instance_id = cloud_attrs.get(
                "_extracted_instance_id",
            ) or cloud_attrs.get("instance_id")
            if cloud_instance_id and instance_id == cloud_instance_id:
                return MatchEvidence(
                    match_type="provider_id",
                    matched_values={
                        "provider_id": provider_id,
                        "instance_id": instance_id,
                    },
                    confidence=1.0,
                    auto_confirm=True,
                )
            # Fall back to matching by name (instance name = entity name)
            if instance_id == cloud_entity.name:
                return MatchEvidence(
                    match_type="provider_id",
                    matched_values={
                        "provider_id": provider_id,
                        "instance_id": instance_id,
                    },
                    confidence=1.0,
                    auto_confirm=True,
                )
            return None

        # Try Azure AKS format
        azure_result = self._parse_azure(provider_id)
        if azure_result:
            _subscription, _resource_group, vm_identifier = azure_result
            # Match against Azure VM identifier in cloud entity attributes
            cloud_attrs = cloud_entity.raw_attributes or {}
            cloud_instance_id = cloud_attrs.get(
                "_extracted_instance_id",
            ) or cloud_attrs.get("instance_id")
            if cloud_instance_id and vm_identifier == cloud_instance_id:
                return MatchEvidence(
                    match_type="provider_id",
                    matched_values={
                        "provider_id": provider_id,
                        "vm_identifier": vm_identifier,
                    },
                    confidence=1.0,
                    auto_confirm=True,
                )
            # Fall back to matching by name
            if vm_identifier == cloud_entity.name:
                return MatchEvidence(
                    match_type="provider_id",
                    matched_values={
                        "provider_id": provider_id,
                        "vm_identifier": vm_identifier,
                    },
                    confidence=1.0,
                    auto_confirm=True,
                )
            return None

        return None

    def _extract_provider_id(self, entity: TopologyEntityModel) -> str | None:
        """
        Extract providerID from entity raw_attributes.

        Checks in order:
        1. _extracted_provider_id (normalized by extraction framework)
        2. spec.providerID (nested K8s API format)
        3. provider_id (flat format)
        """
        attrs = entity.raw_attributes or {}
        if not attrs:
            return None

        # Check extracted format first
        pid = attrs.get("_extracted_provider_id")
        if pid:
            return pid

        # Check nested K8s API format
        spec = attrs.get("spec")
        if isinstance(spec, dict):
            pid = spec.get("providerID")
            if pid:
                return pid

        # Check flat format
        return attrs.get("provider_id") or None

    def _parse_gce(self, provider_id: str) -> tuple[str, str, str] | None:
        """Parse GCE providerID. Returns (project, zone, vm_name) or None."""
        m = _GCE_PATTERN.match(provider_id)
        if m:
            return m.group(1), m.group(2), m.group(3)
        return None

    def _parse_vsphere(self, provider_id: str) -> tuple[str, str] | None:
        """Parse vSphere providerID. Returns (datacenter, vm_id) or None."""
        m = _VSPHERE_PATTERN.match(provider_id)
        if m:
            return m.group(1), m.group(2)
        return None

    def _parse_aws(self, provider_id: str) -> tuple[str, str] | None:
        """Parse AWS EKS providerID. Returns (availability_zone, instance_id) or None."""
        m = _AWS_PATTERN.match(provider_id)
        if m:
            return m.group(1), m.group(2)
        return None

    def _parse_azure(self, provider_id: str) -> tuple[str, str, str] | None:
        """Parse Azure AKS providerID. Returns (subscription, resource_group, vm_identifier) or None."""
        m = _AZURE_PATTERN.match(provider_id)
        if m:
            subscription = m.group(1)
            resource_group = m.group(2)
            # VMSS instance: groups 3+4 are set; standalone VM: group 5 is set
            if m.group(3):
                vm_identifier = f"{m.group(3)}_{m.group(4)}"  # vmss-name_instance-id
            else:
                vm_identifier = m.group(5)
            return subscription, resource_group, vm_identifier
        return None
