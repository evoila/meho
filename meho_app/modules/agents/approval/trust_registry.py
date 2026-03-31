# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Static trust tier maps for typed connectors.

Phase 5: Each map lists operations that are NOT the default (WRITE).
- READ operations: listed explicitly (auto-approved)
- DESTRUCTIVE operations: listed explicitly (red modal)
- Everything else: WRITE (yellow modal, per fail-safe default)

Lookup is case-insensitive on both connector_type and operation_id.
"""

from __future__ import annotations

from meho_app.modules.agents.models import TrustTier

_REGISTRY: dict[str, dict[str, TrustTier]] = {
    "kubernetes": {
        # READ operations
        "list_namespaces": TrustTier.READ,
        "list_pods": TrustTier.READ,
        "list_services": TrustTier.READ,
        "list_deployments": TrustTier.READ,
        "list_nodes": TrustTier.READ,
        "get_pod": TrustTier.READ,
        "get_deployment": TrustTier.READ,
        "get_service": TrustTier.READ,
        "get_node": TrustTier.READ,
        "get_pod_logs": TrustTier.READ,
        "get_events": TrustTier.READ,
        # DESTRUCTIVE operations
        "delete_pod": TrustTier.DESTRUCTIVE,
        "delete_deployment": TrustTier.DESTRUCTIVE,
        "delete_service": TrustTier.DESTRUCTIVE,
        "delete_namespace": TrustTier.DESTRUCTIVE,
        # Everything else (scale, create, update, restart) -> WRITE by default
    },
    "vmware": {
        # READ operations (explicit overrides; most get_/list_ ops covered by name heuristic)
        "list_virtual_machines": TrustTier.READ,
        "list_datastores": TrustTier.READ,
        "list_hosts": TrustTier.READ,
        "list_clusters": TrustTier.READ,
        "get_virtual_machine": TrustTier.READ,
        "get_host": TrustTier.READ,
        # DESTRUCTIVE operations
        "destroy_vm": TrustTier.DESTRUCTIVE,
        "delete_snapshot": TrustTier.DESTRUCTIVE,
    },
    "proxmox": {
        # READ operations
        "list_nodes": TrustTier.READ,
        "list_vms": TrustTier.READ,
        "list_containers": TrustTier.READ,
        "get_node_status": TrustTier.READ,
        "get_vm_status": TrustTier.READ,
        "get_storage": TrustTier.READ,
        # DESTRUCTIVE operations
        "delete_vm": TrustTier.DESTRUCTIVE,
        "delete_container": TrustTier.DESTRUCTIVE,
    },
    "gcp": {
        # READ operations
        "list_instances": TrustTier.READ,
        "list_clusters": TrustTier.READ,
        "get_instance": TrustTier.READ,
        "get_cluster": TrustTier.READ,
        "list_metric_descriptors": TrustTier.READ,
        # Cloud Build READ operations
        "list_builds": TrustTier.READ,
        "get_build": TrustTier.READ,
        "list_build_triggers": TrustTier.READ,
        "get_build_logs": TrustTier.READ,
        # Cloud Build WRITE operations (require approval)
        "cancel_build": TrustTier.WRITE,
        "retry_build": TrustTier.WRITE,
        # Artifact Registry READ operations
        "list_artifact_repositories": TrustTier.READ,
        "list_docker_images": TrustTier.READ,
        # DESTRUCTIVE operations
        "delete_instance": TrustTier.DESTRUCTIVE,
        "delete_cluster": TrustTier.DESTRUCTIVE,
    },
    "argocd": {
        # READ operations (8)
        "list_applications": TrustTier.READ,
        "get_application": TrustTier.READ,
        "get_resource_tree": TrustTier.READ,
        "get_sync_history": TrustTier.READ,
        "get_managed_resources": TrustTier.READ,
        "get_application_events": TrustTier.READ,
        "get_revision_metadata": TrustTier.READ,
        "get_server_diff": TrustTier.READ,
        # WRITE operations (1)
        "sync_application": TrustTier.WRITE,
        # DESTRUCTIVE operations (1)
        "rollback_application": TrustTier.DESTRUCTIVE,
    },
    "github": {
        # READ operations (11)
        "list_repositories": TrustTier.READ,
        "list_commits": TrustTier.READ,
        "compare_refs": TrustTier.READ,
        "list_pull_requests": TrustTier.READ,
        "get_pull_request": TrustTier.READ,
        "list_workflow_runs": TrustTier.READ,
        "get_workflow_run": TrustTier.READ,
        "list_workflow_jobs": TrustTier.READ,
        "get_workflow_logs": TrustTier.READ,
        "list_deployments": TrustTier.READ,
        "get_commit_status": TrustTier.READ,
        # WRITE operations (1)
        "rerun_failed_jobs": TrustTier.WRITE,
    },
}


def get_tier(connector_type: str, operation_id: str) -> TrustTier | None:
    """Look up trust tier for a typed connector operation.

    Args:
        connector_type: Connector type identifier (e.g., "kubernetes", "vmware").
        operation_id: Operation identifier (e.g., "list_pods", "delete_vm").

    Returns:
        TrustTier if the operation is registered, None otherwise.
        Caller should use default WRITE when None is returned.
    """
    type_map = _REGISTRY.get(connector_type.lower())
    if not type_map:
        return None
    return type_map.get(operation_id.lower())
