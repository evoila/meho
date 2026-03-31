# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ArgoCD Response Serializers.

Pure functions for converting ArgoCD API responses (camelCase) to MEHO format
(snake_case). No side effects, no I/O -- just dict -> dict transformations.

ArgoCD's REST API is auto-generated from protobuf via gRPC-gateway, so all
field names use camelCase. These serializers handle the conversion.
"""


def serialize_application_summary(app: dict) -> dict:
    """
    Serialize an ArgoCD application to summary format.

    Extracts key fields from ArgoCD's application response: identity,
    sync/health status, destination, and source information.
    Handles multi-source apps (sources array) as fallback.
    """
    metadata = app.get("metadata", {})
    spec = app.get("spec", {})
    status = app.get("status", {})
    dest = spec.get("destination", {})

    # Handle single-source and multi-source apps
    source = spec.get("source") or {}
    if not source:
        sources = spec.get("sources") or []
        source = sources[0] if sources else {}

    sync_status = status.get("sync", {})
    health_status = status.get("health", {})

    return {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "project": spec.get("project"),
        "sync_status": sync_status.get("status"),
        "health_status": health_status.get("status"),
        "health_message": health_status.get("message"),
        "destination_server": dest.get("server"),
        "destination_namespace": dest.get("namespace"),
        "source_repo": source.get("repoURL"),
        "source_path": source.get("path"),
        "source_target_revision": source.get("targetRevision"),
        "created_at": metadata.get("creationTimestamp"),
    }


def serialize_application_detail(app: dict) -> dict:
    """
    Serialize an ArgoCD application to detailed format.

    Includes everything from summary plus: conditions, operation state,
    images, and resource count.
    """
    # Start with summary fields
    result = serialize_application_summary(app)

    status = app.get("status", {})

    # Conditions (type, message, lastTransitionTime)
    conditions = status.get("conditions") or []
    result["conditions"] = [
        {
            "type": c.get("type"),
            "message": c.get("message"),
            "last_transition_time": c.get("lastTransitionTime"),
        }
        for c in conditions
    ]

    # Operation state (phase, message, revision from syncResult)
    op_state = status.get("operationState") or {}
    sync_result = op_state.get("syncResult") or {}
    result["operation_state"] = (
        {
            "phase": op_state.get("phase"),
            "message": op_state.get("message"),
            "revision": sync_result.get("revision"),
        }
        if op_state
        else None
    )

    # Images from summary
    summary = status.get("summary") or {}
    result["images"] = summary.get("images") or []

    # Resource count
    resources = status.get("resources") or []
    result["resource_count"] = len(resources)

    return result


def _serialize_node(node: dict) -> dict:
    """
    Serialize a single resource tree node.

    Extracts kind, name, namespace, group, version, health, and creation time
    from the ArgoCD resource tree node structure.
    """
    ref = node.get("resourceRef", {})
    health = node.get("health", {})
    return {
        "kind": ref.get("kind"),
        "name": ref.get("name"),
        "namespace": ref.get("namespace"),
        "group": ref.get("group"),
        "version": ref.get("version"),
        "health_status": health.get("status"),
        "health_message": health.get("message"),
        "created_at": node.get("createdAt"),
    }


def serialize_resource_tree(data: dict, max_depth: int = 2) -> dict:
    """
    Serialize resource tree with depth limiting.

    Parses the flat nodes array from the ArgoCD API response, builds
    parent-child relationships using parentRefs, and returns top-level
    resources plus one level of children.

    Top-level nodes = nodes with no parentRefs or parentRefs pointing
    to the Application itself (which is not in the nodes array).
    """
    nodes = data.get("nodes", [])

    # Build lookup: uid -> node and parent_uid -> children
    uid_lookup = {}
    for node in nodes:
        ref = node.get("resourceRef", {})
        uid = ref.get("uid")
        if uid:
            uid_lookup[uid] = node

    # Separate top-level nodes and children
    top_level = []
    children_map: dict[str, list] = {}

    for node in nodes:
        parent_refs = node.get("parentRefs") or []

        if not parent_refs:
            # No parents -- top-level resource
            top_level.append(node)
        else:
            # Check if any parent is in our node set (not the Application itself)
            has_known_parent = False
            for parent in parent_refs:
                parent_uid = parent.get("uid")
                if parent_uid and parent_uid in uid_lookup:
                    children_map.setdefault(parent_uid, []).append(node)
                    has_known_parent = True

            if not has_known_parent:
                # Parent is the Application (not in nodes) -- treat as top-level
                top_level.append(node)

    # Serialize top-level + one level of children
    result = []
    for node in top_level:
        serialized = _serialize_node(node)
        uid = node.get("resourceRef", {}).get("uid")
        if uid and uid in children_map:
            serialized["children"] = [_serialize_node(child) for child in children_map[uid]]
        result.append(serialized)

    return {"resources": result, "total_nodes": len(nodes)}


def serialize_sync_history(app: dict, max_entries: int = 10) -> list:
    """
    Serialize sync history entries from application status.

    Extracts the history array from the application's status and returns
    deployment entries with deployment_id (for rollback), revision, timestamp,
    and source info. History is newest-first.
    """
    status = app.get("status", {})
    history = status.get("history") or []

    # History is already newest-first from ArgoCD
    entries = history[:max_entries]

    result = []
    for entry in entries:
        source = entry.get("source") or {}
        result.append(
            {
                "deployment_id": entry.get("id"),
                "revision": entry.get("revision"),
                "deployed_at": entry.get("deployedAt"),
                "source_repo": source.get("repoURL"),
                "source_path": source.get("path"),
                "source_target_revision": source.get("targetRevision"),
            }
        )

    return result


def serialize_managed_resources(data: dict) -> list:
    """
    Serialize managed resources with diff status for drift detection.

    Each managed resource includes sync status, health status, and a flag
    indicating whether live state differs from desired state.
    """
    items = data.get("items") or []

    result = []
    for item in items:
        health = item.get("health") or {}
        # Diff is present if the resource has drifted (live != desired)
        diff = item.get("diff") or {}
        has_diff = bool(diff.get("diff") or diff.get("normalizedLiveState"))

        result.append(
            {
                "group": item.get("group"),
                "kind": item.get("kind"),
                "namespace": item.get("namespace"),
                "name": item.get("name"),
                "sync_status": item.get("status"),
                "health_status": health.get("status"),
                "health_message": health.get("message"),
                "has_diff": has_diff,
            }
        )

    return result


def serialize_events(data: dict) -> list:
    """
    Serialize K8s events for application resources.

    Extracts event type (Normal/Warning), reason, message, and the involved
    resource information.
    """
    items = data.get("items") or []

    result = []
    for event in items:
        involved = event.get("involvedObject") or {}
        result.append(
            {
                "type": event.get("type"),
                "reason": event.get("reason"),
                "message": event.get("message"),
                "resource_kind": involved.get("kind"),
                "resource_name": involved.get("name"),
                "resource_namespace": involved.get("namespace"),
                "first_seen": event.get("firstTimestamp"),
                "last_seen": event.get("lastTimestamp"),
                "count": event.get("count"),
            }
        )

    return result


def serialize_revision_metadata(data: dict) -> dict:
    """
    Serialize revision metadata (git commit info).

    Returns author, date, commit message, and tags for a deployed revision.
    """
    return {
        "author": data.get("author"),
        "date": data.get("date"),
        "message": data.get("message"),
        "tags": data.get("tags") or [],
    }


def serialize_server_diff(data: dict) -> list:
    """
    Serialize server-side diff results.

    Each item represents a resource with differences between live state
    and desired state. Includes the diff text and status (added/modified/removed).
    """
    items = data.get("items") or []

    result = []
    for item in items:
        target_state = item.get("targetState") or ""
        live_state = item.get("liveState") or ""
        diff_obj = item.get("diff") or {}
        diff_text = diff_obj.get("diff") or ""

        # Determine status based on presence of states
        if not live_state or live_state == "null":
            status = "added"
        elif not target_state or target_state == "null":
            status = "removed"
        else:
            status = "modified"

        result.append(
            {
                "kind": item.get("kind"),
                "namespace": item.get("namespace"),
                "name": item.get("name"),
                "diff_text": diff_text,
                "status": status,
            }
        )

    return result


def serialize_sync_result(data: dict) -> dict:
    """
    Serialize sync/rollback operation result.

    Extracts the resulting sync status, operation state, and timing from
    the ArgoCD response after a sync or rollback operation.
    """
    status = data.get("status", {})
    sync_info = status.get("sync") or {}
    op_state = status.get("operationState") or {}

    return {
        "sync_status": sync_info.get("status"),
        "revision": sync_info.get("revision"),
        "operation_phase": op_state.get("phase"),
        "operation_message": op_state.get("message"),
        "started_at": op_state.get("startedAt"),
        "finished_at": op_state.get("finishedAt"),
    }
