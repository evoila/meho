# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tests for the trust classification pipeline.

Phase 5 Plan 01: Three-tier trust classification (READ, WRITE, DESTRUCTIVE)
covering REST HTTP method heuristic, typed connector registry, per-endpoint
overrides, and fail-safe defaults.
"""

import pytest

from meho_app.modules.agents.approval.trust_classifier import (
    _classify_by_name,
    classify_operation,
    requires_approval,
)
from meho_app.modules.agents.approval.trust_registry import get_tier
from meho_app.modules.agents.models import DANGER_LEVEL_TO_TRUST, DangerLevel, TrustTier

# =============================================================================
# TrustTier enum tests
# =============================================================================


class TestTrustTierEnum:
    """Verify TrustTier enum structure and values."""

    def test_has_exactly_three_values(self):
        assert len(TrustTier) == 3

    def test_values_are_lowercase_strings(self):
        assert TrustTier.READ.value == "read"
        assert TrustTier.WRITE.value == "write"
        assert TrustTier.DESTRUCTIVE.value == "destructive"

    def test_is_str_enum(self):
        """TrustTier is a str enum so it can be used as a string directly."""
        assert isinstance(TrustTier.READ, str)
        assert TrustTier.READ == "read"
        assert TrustTier.WRITE == "write"
        assert TrustTier.DESTRUCTIVE == "destructive"


# =============================================================================
# classify_operation() -- REST HTTP method heuristic
# =============================================================================


class TestClassifyOperationHTTPMethod:
    """REST connector classification via HTTP method heuristic."""

    def test_get_is_read(self):
        assert classify_operation("rest", "get_users", http_method="GET") == TrustTier.READ

    def test_head_is_read(self):
        assert classify_operation("rest", "check_health", http_method="HEAD") == TrustTier.READ

    def test_options_is_read(self):
        assert classify_operation("rest", "preflight", http_method="OPTIONS") == TrustTier.READ

    def test_post_is_write(self):
        assert classify_operation("rest", "create_user", http_method="POST") == TrustTier.WRITE

    def test_put_is_write(self):
        assert classify_operation("rest", "update_user", http_method="PUT") == TrustTier.WRITE

    def test_patch_is_write(self):
        assert classify_operation("rest", "patch_user", http_method="PATCH") == TrustTier.WRITE

    def test_delete_is_destructive(self):
        assert (
            classify_operation("rest", "delete_user", http_method="DELETE") == TrustTier.DESTRUCTIVE
        )

    def test_unknown_method_is_write_failsafe(self):
        assert classify_operation("rest", "foobar", http_method="FOOBAR") == TrustTier.WRITE

    def test_no_method_for_rest_is_write_failsafe(self):
        assert classify_operation("rest", "unknown_op") == TrustTier.WRITE

    def test_case_insensitive_method(self):
        """HTTP methods should be case-insensitive."""
        assert classify_operation("rest", "x", http_method="get") == TrustTier.READ
        assert classify_operation("rest", "x", http_method="Post") == TrustTier.WRITE
        assert classify_operation("rest", "x", http_method="delete") == TrustTier.DESTRUCTIVE


# =============================================================================
# classify_operation() -- typed connector registry
# =============================================================================


class TestClassifyOperationTypedConnectors:
    """Typed connector classification via static registry lookup."""

    # Kubernetes
    def test_kubernetes_list_pods_is_read(self):
        assert classify_operation("kubernetes", "list_pods") == TrustTier.READ

    def test_kubernetes_delete_pod_is_destructive(self):
        assert classify_operation("kubernetes", "delete_pod") == TrustTier.DESTRUCTIVE

    def test_kubernetes_unregistered_op_is_write_default(self):
        """Operations not in registry default to WRITE."""
        assert classify_operation("kubernetes", "scale_deployment") == TrustTier.WRITE

    # VMware
    def test_vmware_list_vms_is_read(self):
        assert classify_operation("vmware", "list_virtual_machines") == TrustTier.READ

    def test_vmware_destroy_vm_is_destructive(self):
        assert classify_operation("vmware", "destroy_vm") == TrustTier.DESTRUCTIVE

    # Proxmox
    def test_proxmox_list_nodes_is_read(self):
        assert classify_operation("proxmox", "list_nodes") == TrustTier.READ

    def test_proxmox_delete_vm_is_destructive(self):
        assert classify_operation("proxmox", "delete_vm") == TrustTier.DESTRUCTIVE

    # GCP
    def test_gcp_list_instances_is_read(self):
        assert classify_operation("gcp", "list_instances") == TrustTier.READ

    def test_gcp_delete_instance_is_destructive(self):
        assert classify_operation("gcp", "delete_instance") == TrustTier.DESTRUCTIVE


# =============================================================================
# _classify_by_name() -- operation name heuristic
# =============================================================================


class TestClassifyByNameHeuristic:
    """Operation name prefix heuristic (step 3 in classification chain)."""

    # READ prefixes
    @pytest.mark.parametrize(
        "op_id",
        [
            "list_pods",
            "get_cluster_status",
            "describe_pod",
            "search_inventory",
            "browse_datastore",
            "query_used_vlans",
            "export_vm",
            "find_rules_for_vm",
            "retrieve_hardware_uptime",
            "acquire_mks_ticket",
            "download_file_from_guest",
            "place_vm",
            "recommend_hosts_for_vm",
        ],
    )
    def test_read_prefixes(self, op_id: str):
        assert _classify_by_name(op_id) == TrustTier.READ

    # DESTRUCTIVE prefixes
    @pytest.mark.parametrize(
        "op_id",
        [
            "delete_vm",
            "destroy_folder",
            "remove_network_adapter",
            "unregister_vm",
        ],
    )
    def test_destructive_prefixes(self, op_id: str):
        assert _classify_by_name(op_id) == TrustTier.DESTRUCTIVE

    # No-match falls through (returns None)
    @pytest.mark.parametrize(
        "op_id",
        [
            "start_vm",
            "stop_container",
            "clone_vm",
            "migrate_vm",
            "scale_deployment",
            "restart_deployment",
            "shutdown_guest",
            "power_on_vm",
            "suspend_vm",
            "resume_vm",
        ],
    )
    def test_write_operations_return_none(self, op_id: str):
        assert _classify_by_name(op_id) is None

    def test_case_insensitive(self):
        assert _classify_by_name("GET_Cluster_Status") == TrustTier.READ
        assert _classify_by_name("DELETE_VM") == TrustTier.DESTRUCTIVE


class TestClassifyOperationNameHeuristicIntegration:
    """End-to-end: name heuristic fills gaps in the static registry."""

    def test_proxmox_get_cluster_status_is_read(self):
        """The bug that started this: get_cluster_status was defaulting to WRITE."""
        assert classify_operation("proxmox", "get_cluster_status") == TrustTier.READ

    def test_proxmox_get_cluster_resources_is_read(self):
        assert classify_operation("proxmox", "get_cluster_resources") == TrustTier.READ

    def test_proxmox_list_vm_snapshots_is_read(self):
        assert classify_operation("proxmox", "list_vm_snapshots") == TrustTier.READ

    def test_vmware_get_vcenter_info_is_read(self):
        assert classify_operation("vmware", "get_vcenter_info") == TrustTier.READ

    def test_vmware_list_alarms_is_read(self):
        assert classify_operation("vmware", "list_alarms") == TrustTier.READ

    def test_kubernetes_describe_pod_is_read(self):
        assert classify_operation("kubernetes", "describe_pod") == TrustTier.READ

    def test_kubernetes_list_configmaps_is_read(self):
        assert classify_operation("kubernetes", "list_configmaps") == TrustTier.READ

    def test_gcp_list_disks_is_read(self):
        assert classify_operation("gcp", "list_disks") == TrustTier.READ

    def test_write_operations_still_write(self):
        """Operations without a recognized prefix still default to WRITE."""
        assert classify_operation("proxmox", "start_vm") == TrustTier.WRITE
        assert classify_operation("vmware", "power_on_vm") == TrustTier.WRITE
        assert classify_operation("kubernetes", "scale_deployment") == TrustTier.WRITE

    def test_registry_overrides_heuristic(self):
        """Static registry entries still take priority over the name heuristic."""
        assert classify_operation("kubernetes", "delete_pod") == TrustTier.DESTRUCTIVE

    def test_unknown_connector_uses_heuristic(self):
        """Even unknown connector types benefit from the name heuristic."""
        assert classify_operation("unknown_type", "get_status") == TrustTier.READ
        assert classify_operation("unknown_type", "delete_thing") == TrustTier.DESTRUCTIVE


# =============================================================================
# classify_operation() -- per-endpoint override
# =============================================================================


class TestClassifyOperationOverride:
    """Per-endpoint DB override takes priority over all other classification."""

    def test_override_read_on_post_wins(self):
        result = classify_operation("rest", "search", http_method="POST", override=TrustTier.READ)
        assert result == TrustTier.READ

    def test_override_destructive_on_get_wins(self):
        result = classify_operation(
            "rest", "get_data", http_method="GET", override=TrustTier.DESTRUCTIVE
        )
        assert result == TrustTier.DESTRUCTIVE

    def test_override_write_on_delete_wins(self):
        result = classify_operation(
            "rest", "soft_delete", http_method="DELETE", override=TrustTier.WRITE
        )
        assert result == TrustTier.WRITE

    def test_override_wins_over_registry(self):
        """Override takes precedence over typed connector registry too."""
        result = classify_operation("kubernetes", "list_pods", override=TrustTier.DESTRUCTIVE)
        assert result == TrustTier.DESTRUCTIVE


# =============================================================================
# classify_operation() -- fail-safe defaults
# =============================================================================


class TestClassifyOperationDefaults:
    """Unclassifiable operations default to WRITE (fail-safe)."""

    def test_unknown_connector_no_method_is_write(self):
        assert classify_operation("unknown_type", "unknown_op") == TrustTier.WRITE

    def test_rest_no_method_no_override_is_write(self):
        assert classify_operation("rest", "unknown_op") == TrustTier.WRITE


# =============================================================================
# get_tier() -- trust registry direct tests
# =============================================================================


class TestGetTier:
    """Direct tests for the trust registry lookup function."""

    def test_known_connector_known_operation(self):
        assert get_tier("kubernetes", "list_pods") == TrustTier.READ

    def test_known_connector_unknown_operation(self):
        assert get_tier("kubernetes", "unknown_op") is None

    def test_unknown_connector(self):
        assert get_tier("unknown_type", "any_op") is None

    def test_case_insensitive_connector_type(self):
        assert get_tier("Kubernetes", "list_pods") == TrustTier.READ
        assert get_tier("KUBERNETES", "list_pods") == TrustTier.READ

    def test_case_insensitive_operation_id(self):
        assert get_tier("kubernetes", "List_Pods") == TrustTier.READ
        assert get_tier("kubernetes", "LIST_PODS") == TrustTier.READ

    def test_vmware_operations(self):
        assert get_tier("vmware", "get_virtual_machine") == TrustTier.READ
        assert get_tier("vmware", "destroy_vm") == TrustTier.DESTRUCTIVE
        assert get_tier("vmware", "create_snapshot") is None

    def test_vmware_dead_entries_removed(self):
        """Old entries with wrong names must not appear in the registry."""
        assert get_tier("vmware", "get_vm_details") is None
        assert get_tier("vmware", "get_host_details") is None
        assert get_tier("vmware", "get_performance_metrics") is None

    def test_gcp_metric_descriptor_entry(self):
        """GCP list_metrics was renamed to list_metric_descriptors."""
        assert get_tier("gcp", "list_metric_descriptors") == TrustTier.READ
        assert get_tier("gcp", "list_metrics") is None

    def test_proxmox_operations(self):
        assert get_tier("proxmox", "get_node_status") == TrustTier.READ
        assert get_tier("proxmox", "delete_container") == TrustTier.DESTRUCTIVE

    def test_gcp_operations(self):
        assert get_tier("gcp", "get_cluster") == TrustTier.READ
        assert get_tier("gcp", "delete_cluster") == TrustTier.DESTRUCTIVE


# =============================================================================
# DANGER_LEVEL_TO_TRUST mapping
# =============================================================================


class TestDangerLevelToTrustMapping:
    """Verify the mapping from old four-tier DangerLevel to new three-tier TrustTier."""

    def test_safe_maps_to_read(self):
        assert DANGER_LEVEL_TO_TRUST[DangerLevel.SAFE] == TrustTier.READ

    def test_caution_maps_to_write(self):
        assert DANGER_LEVEL_TO_TRUST[DangerLevel.CAUTION] == TrustTier.WRITE

    def test_dangerous_maps_to_write(self):
        assert DANGER_LEVEL_TO_TRUST[DangerLevel.DANGEROUS] == TrustTier.WRITE

    def test_critical_maps_to_destructive(self):
        assert DANGER_LEVEL_TO_TRUST[DangerLevel.CRITICAL] == TrustTier.DESTRUCTIVE

    def test_mapping_covers_all_danger_levels(self):
        """Every DangerLevel must have a mapping to TrustTier."""
        for level in DangerLevel:
            assert level in DANGER_LEVEL_TO_TRUST


# =============================================================================
# requires_approval() helper
# =============================================================================


class TestRequiresApproval:
    """Helper function that determines if a tier needs operator approval."""

    def test_read_does_not_require_approval(self):
        assert requires_approval(TrustTier.READ) is False

    def test_write_requires_approval(self):
        assert requires_approval(TrustTier.WRITE) is True

    def test_destructive_requires_approval(self):
        assert requires_approval(TrustTier.DESTRUCTIVE) is True
