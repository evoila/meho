# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP Object Serializers (TASK-102)

Convert GCP SDK objects to JSON-serializable dictionaries.
"""

from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.gcp.helpers import (
    extract_name_from_url,
    extract_zone_from_url,
    format_build_duration,
    format_bytes,
    format_protobuf_timestamp,
    format_timestamp,
    parse_labels,
    parse_machine_type,
)

logger = get_logger(__name__)


def serialize_instance(instance: Any) -> dict[str, Any]:
    """
    Serialize a Compute Engine Instance to a dictionary.

    Args:
        instance: google.cloud.compute_v1.Instance

    Returns:
        Serialized instance data
    """
    # Parse machine type
    machine_info = parse_machine_type(instance.machine_type or "")

    # Get network interfaces
    network_interfaces = []
    for nic in instance.network_interfaces or []:
        nic_info = {
            "name": nic.name,
            "network": extract_name_from_url(nic.network or ""),
            "subnetwork": extract_name_from_url(nic.subnetwork or ""),
            "internal_ip": nic.network_i_p,
            "external_ip": None,
        }
        # Get external IP if exists
        for access_config in nic.access_configs or []:
            if access_config.nat_i_p:
                nic_info["external_ip"] = access_config.nat_i_p
                break
        network_interfaces.append(nic_info)

    # Get disks
    disks = []
    for disk in instance.disks or []:
        disk_info = {
            "name": extract_name_from_url(disk.source or ""),
            "boot": disk.boot,
            "auto_delete": disk.auto_delete,
            "mode": disk.mode,
            "type": disk.type_,
            "size_gb": disk.disk_size_gb,
        }
        disks.append(disk_info)

    return {
        "id": str(instance.id),
        "name": instance.name,
        "description": instance.description,
        "zone": extract_zone_from_url(instance.zone or ""),
        "machine_type": machine_info.get("machine_type", ""),
        "status": instance.status,
        "status_message": instance.status_message,
        "creation_timestamp": format_timestamp(instance.creation_timestamp),
        "last_start_timestamp": format_timestamp(instance.last_start_timestamp),
        "last_stop_timestamp": format_timestamp(instance.last_stop_timestamp),
        "cpu_platform": instance.cpu_platform,
        "labels": parse_labels(instance.labels),
        "network_interfaces": network_interfaces,
        "disks": disks,
        "can_ip_forward": instance.can_ip_forward,
        "deletion_protection": instance.deletion_protection,
        "fingerprint": instance.fingerprint,
        "self_link": instance.self_link,
    }


def serialize_disk(disk: Any) -> dict[str, Any]:
    """
    Serialize a Compute Engine Disk to a dictionary.

    Args:
        disk: google.cloud.compute_v1.Disk

    Returns:
        Serialized disk data
    """
    return {
        "id": str(disk.id),
        "name": disk.name,
        "description": disk.description,
        "zone": extract_zone_from_url(disk.zone or ""),
        "size_gb": int(disk.size_gb) if disk.size_gb else 0,
        "size_formatted": format_bytes(int(disk.size_gb or 0) * 1024 * 1024 * 1024),
        "type": extract_name_from_url(disk.type_ or ""),
        "status": disk.status,
        "source_image": extract_name_from_url(disk.source_image or ""),
        "source_snapshot": extract_name_from_url(disk.source_snapshot or ""),
        "users": [extract_name_from_url(u) for u in (disk.users or [])],
        "labels": parse_labels(disk.labels),
        "creation_timestamp": format_timestamp(disk.creation_timestamp),
        "last_attach_timestamp": format_timestamp(disk.last_attach_timestamp),
        "last_detach_timestamp": format_timestamp(disk.last_detach_timestamp),
        "physical_block_size_bytes": disk.physical_block_size_bytes,
        "self_link": disk.self_link,
    }


def serialize_snapshot(snapshot: Any) -> dict[str, Any]:
    """
    Serialize a Compute Engine Snapshot to a dictionary.

    Args:
        snapshot: google.cloud.compute_v1.Snapshot

    Returns:
        Serialized snapshot data
    """
    return {
        "id": str(snapshot.id),
        "name": snapshot.name,
        "description": snapshot.description,
        "source_disk": extract_name_from_url(snapshot.source_disk or ""),
        "disk_size_gb": int(snapshot.disk_size_gb) if snapshot.disk_size_gb else 0,
        "storage_bytes": int(snapshot.storage_bytes) if snapshot.storage_bytes else 0,
        "storage_formatted": format_bytes(int(snapshot.storage_bytes or 0)),
        "status": snapshot.status,
        "storage_locations": list(snapshot.storage_locations or []),
        "labels": parse_labels(snapshot.labels),
        "creation_timestamp": format_timestamp(snapshot.creation_timestamp),
        "auto_created": snapshot.auto_created,
        "self_link": snapshot.self_link,
    }


def serialize_network(network: Any) -> dict[str, Any]:
    """
    Serialize a VPC Network to a dictionary.

    Args:
        network: google.cloud.compute_v1.Network

    Returns:
        Serialized network data
    """
    return {
        "id": str(network.id),
        "name": network.name,
        "description": network.description,
        "auto_create_subnetworks": network.auto_create_subnetworks,
        "routing_mode": network.routing_config.routing_mode if network.routing_config else None,
        "mtu": network.mtu,
        "subnetworks": [extract_name_from_url(s) for s in (network.subnetworks or [])],
        "peerings": [
            {
                "name": p.name,
                "network": extract_name_from_url(p.network or ""),
                "state": p.state,
            }
            for p in (network.peerings or [])
        ],
        "creation_timestamp": format_timestamp(network.creation_timestamp),
        "self_link": network.self_link,
    }


def serialize_subnetwork(subnetwork: Any) -> dict[str, Any]:
    """
    Serialize a Subnetwork to a dictionary.

    Args:
        subnetwork: google.cloud.compute_v1.Subnetwork

    Returns:
        Serialized subnetwork data
    """
    return {
        "id": str(subnetwork.id),
        "name": subnetwork.name,
        "description": subnetwork.description,
        "network": extract_name_from_url(subnetwork.network or ""),
        "region": extract_name_from_url(subnetwork.region or ""),
        "ip_cidr_range": subnetwork.ip_cidr_range,
        "gateway_address": subnetwork.gateway_address,
        "private_ip_google_access": subnetwork.private_ip_google_access,
        "purpose": subnetwork.purpose,
        "role": subnetwork.role,
        "state": subnetwork.state,
        "secondary_ip_ranges": [
            {
                "range_name": r.range_name,
                "ip_cidr_range": r.ip_cidr_range,
            }
            for r in (subnetwork.secondary_ip_ranges or [])
        ],
        "creation_timestamp": format_timestamp(subnetwork.creation_timestamp),
        "self_link": subnetwork.self_link,
    }


def serialize_firewall(firewall: Any) -> dict[str, Any]:
    """
    Serialize a Firewall Rule to a dictionary.

    Args:
        firewall: google.cloud.compute_v1.Firewall

    Returns:
        Serialized firewall data
    """
    return {
        "id": str(firewall.id),
        "name": firewall.name,
        "description": firewall.description,
        "network": extract_name_from_url(firewall.network or ""),
        "priority": firewall.priority,
        "direction": firewall.direction,
        "disabled": firewall.disabled,
        "source_ranges": list(firewall.source_ranges or []),
        "destination_ranges": list(firewall.destination_ranges or []),
        "source_tags": list(firewall.source_tags or []),
        "target_tags": list(firewall.target_tags or []),
        "source_service_accounts": list(firewall.source_service_accounts or []),
        "target_service_accounts": list(firewall.target_service_accounts or []),
        "allowed": [
            {
                "protocol": a.I_p_protocol,
                "ports": list(a.ports or []),
            }
            for a in (firewall.allowed or [])
        ],
        "denied": [
            {
                "protocol": d.I_p_protocol,
                "ports": list(d.ports or []),
            }
            for d in (firewall.denied or [])
        ],
        "log_config": {
            "enable": firewall.log_config.enable if firewall.log_config else False,
        },
        "creation_timestamp": format_timestamp(firewall.creation_timestamp),
        "self_link": firewall.self_link,
    }


def serialize_cluster(cluster: Any) -> dict[str, Any]:  # NOSONAR (cognitive complexity)
    """
    Serialize a GKE Cluster to a dictionary.

    Args:
        cluster: google.cloud.container_v1.Cluster

    Returns:
        Serialized cluster data
    """
    # Get node pools info
    node_pools = []
    for np in cluster.node_pools or []:
        node_pools.append(
            {
                "name": np.name,
                "status": np.status.name if np.status else None,
                "initial_node_count": np.initial_node_count,
                "machine_type": np.config.machine_type if np.config else None,
                "disk_size_gb": np.config.disk_size_gb if np.config else None,
                "disk_type": np.config.disk_type if np.config else None,
                "autoscaling": {
                    "enabled": np.autoscaling.enabled if np.autoscaling else False,
                    "min_node_count": np.autoscaling.min_node_count if np.autoscaling else 0,
                    "max_node_count": np.autoscaling.max_node_count if np.autoscaling else 0,
                }
                if np.autoscaling
                else None,
            }
        )

    return {
        "name": cluster.name,
        "description": cluster.description,
        "location": cluster.location,
        "status": cluster.status.name if cluster.status else None,
        "status_message": cluster.status_message,
        "current_master_version": cluster.current_master_version,
        "current_node_version": cluster.current_node_version,
        "current_node_count": cluster.current_node_count,
        "endpoint": cluster.endpoint,
        "initial_cluster_version": cluster.initial_cluster_version,
        "node_pools": node_pools,
        "network": cluster.network,
        "subnetwork": cluster.subnetwork,
        "cluster_ipv4_cidr": cluster.cluster_ipv4_cidr,
        "services_ipv4_cidr": cluster.services_ipv4_cidr,
        "labels": dict(cluster.resource_labels) if cluster.resource_labels else {},
        "legacy_abac_enabled": cluster.legacy_abac.enabled if cluster.legacy_abac else False,
        "master_authorized_networks_enabled": (
            cluster.master_authorized_networks_config.enabled
            if cluster.master_authorized_networks_config
            else False
        ),
        "create_time": cluster.create_time,
        "expire_time": cluster.expire_time,
        "self_link": cluster.self_link,
    }


def serialize_node_pool(node_pool: Any) -> dict[str, Any]:
    """
    Serialize a GKE Node Pool to a dictionary.

    Args:
        node_pool: google.cloud.container_v1.NodePool

    Returns:
        Serialized node pool data
    """
    config = node_pool.config or type("Config", (), {})()
    autoscaling = node_pool.autoscaling or type("Autoscaling", (), {"enabled": False})()
    management = node_pool.management or type("Management", (), {})()

    return {
        "name": node_pool.name,
        "status": node_pool.status.name if node_pool.status else None,
        "status_message": node_pool.status_message,
        "initial_node_count": node_pool.initial_node_count,
        "config": {
            "machine_type": getattr(config, "machine_type", None),
            "disk_size_gb": getattr(config, "disk_size_gb", None),
            "disk_type": getattr(config, "disk_type", None),
            "image_type": getattr(config, "image_type", None),
            "preemptible": getattr(config, "preemptible", False),
            "spot": getattr(config, "spot", False),
            "labels": dict(getattr(config, "labels", {}) or {}),
            "oauth_scopes": list(getattr(config, "oauth_scopes", []) or []),
        },
        "autoscaling": {
            "enabled": getattr(autoscaling, "enabled", False),
            "min_node_count": getattr(autoscaling, "min_node_count", 0),
            "max_node_count": getattr(autoscaling, "max_node_count", 0),
            "total_min_node_count": getattr(autoscaling, "total_min_node_count", 0),
            "total_max_node_count": getattr(autoscaling, "total_max_node_count", 0),
        },
        "management": {
            "auto_upgrade": getattr(management, "auto_upgrade", False),
            "auto_repair": getattr(management, "auto_repair", False),
        },
        "version": node_pool.version,
        "self_link": node_pool.self_link,
    }


def serialize_metric_descriptor(
    descriptor: Any,
) -> dict[str, Any]:  # NOSONAR (cognitive complexity)
    """
    Serialize a Cloud Monitoring MetricDescriptor to a dictionary.

    Args:
        descriptor: google.cloud.monitoring_v3.MetricDescriptor

    Returns:
        Serialized metric descriptor data
    """
    # Safely extract type - handle protobuf naming quirks
    metric_type = getattr(descriptor, "type_", None) or getattr(descriptor, "type", None)

    # Safely extract enum names
    metric_kind = None
    value_type = None
    try:
        if descriptor.metric_kind:
            metric_kind = (
                descriptor.metric_kind.name
                if hasattr(descriptor.metric_kind, "name")
                else str(descriptor.metric_kind)
            )
        if descriptor.value_type:
            value_type = (
                descriptor.value_type.name
                if hasattr(descriptor.value_type, "name")
                else str(descriptor.value_type)
            )
    except Exception as e:
        logger.warning(f"Failed to serialize metric kind/value type: {e}")

    # Safely extract labels
    labels = []
    try:
        for label in descriptor.labels or []:
            label_value_type = None
            if label.value_type:
                label_value_type = (
                    label.value_type.name
                    if hasattr(label.value_type, "name")
                    else str(label.value_type)
                )
            labels.append(
                {
                    "key": label.key,
                    "value_type": label_value_type,
                    "description": label.description,
                }
            )
    except Exception as e:
        logger.warning(f"Failed to serialize descriptor labels: {e}")

    return {
        "name": descriptor.name,
        "type": metric_type,
        "display_name": descriptor.display_name,
        "description": descriptor.description,
        "metric_kind": metric_kind,
        "value_type": value_type,
        "unit": descriptor.unit,
        "labels": labels,
    }


def serialize_time_series(time_series: Any) -> dict[str, Any]:  # NOSONAR (cognitive complexity)
    """
    Serialize a Cloud Monitoring TimeSeries to a dictionary.

    Args:
        time_series: google.cloud.monitoring_v3.TimeSeries

    Returns:
        Serialized time series data
    """
    points = []
    for point in time_series.points or []:
        value = None
        if point.value:
            if point.value.double_value is not None:
                value = point.value.double_value
            elif point.value.int64_value is not None:
                value = point.value.int64_value
            elif point.value.bool_value is not None:
                value = point.value.bool_value
            elif point.value.string_value:
                value = point.value.string_value

        points.append(
            {
                "value": value,
                "interval": {
                    "start_time": point.interval.start_time.isoformat()
                    if point.interval and point.interval.start_time
                    else None,
                    "end_time": point.interval.end_time.isoformat()
                    if point.interval and point.interval.end_time
                    else None,
                },
            }
        )

    # Safely extract metric type - handle both protobuf objects and raw values
    metric_type = None
    metric_labels = {}
    if time_series.metric:
        try:
            metric_type = getattr(time_series.metric, "type_", None) or getattr(
                time_series.metric, "type", None
            )
            metric_labels = dict(time_series.metric.labels) if time_series.metric.labels else {}
        except Exception as e:
            logger.warning(f"Failed to serialize metric: {e}")

    # Safely extract resource type
    resource_type = None
    resource_labels = {}
    if time_series.resource:
        try:
            resource_type = getattr(time_series.resource, "type_", None) or getattr(
                time_series.resource, "type", None
            )
            resource_labels = (
                dict(time_series.resource.labels) if time_series.resource.labels else {}
            )
        except Exception as e:
            logger.warning(f"Failed to serialize resource: {e}")

    # Safely extract enum names
    metric_kind = None
    value_type = None
    try:
        if time_series.metric_kind:
            metric_kind = (
                time_series.metric_kind.name
                if hasattr(time_series.metric_kind, "name")
                else str(time_series.metric_kind)
            )
        if time_series.value_type:
            value_type = (
                time_series.value_type.name
                if hasattr(time_series.value_type, "name")
                else str(time_series.value_type)
            )
    except Exception as e:
        logger.warning(f"Failed to serialize metric kind/value type: {e}")

    return {
        "metric": {
            "type": metric_type,
            "labels": metric_labels,
        },
        "resource": {
            "type": resource_type,
            "labels": resource_labels,
        },
        "metric_kind": metric_kind,
        "value_type": value_type,
        "points": points,
    }


def serialize_alert_policy(policy: Any) -> dict[str, Any]:
    """
    Serialize a Cloud Monitoring AlertPolicy to a dictionary.

    Args:
        policy: google.cloud.monitoring_v3.AlertPolicy

    Returns:
        Serialized alert policy data
    """
    conditions = []
    for cond in policy.conditions or []:
        conditions.append(
            {
                "name": cond.name,
                "display_name": cond.display_name,
            }
        )

    return {
        "name": policy.name,
        "display_name": policy.display_name,
        "documentation": {
            "content": policy.documentation.content if policy.documentation else None,
            "mime_type": policy.documentation.mime_type if policy.documentation else None,
        },
        "enabled": policy.enabled.value if hasattr(policy.enabled, "value") else policy.enabled,
        "conditions": conditions,
        "combiner": policy.combiner.name if policy.combiner else None,
        "notification_channels": list(policy.notification_channels or []),
        "creation_record": {
            "mutate_time": policy.creation_record.mutate_time.isoformat()
            if policy.creation_record and policy.creation_record.mutate_time
            else None,
            "mutated_by": policy.creation_record.mutated_by if policy.creation_record else None,
        },
        "mutation_record": {
            "mutate_time": policy.mutation_record.mutate_time.isoformat()
            if policy.mutation_record and policy.mutation_record.mutate_time
            else None,
            "mutated_by": policy.mutation_record.mutated_by if policy.mutation_record else None,
        },
    }


# =========================================================================
# CLOUD BUILD SERIALIZERS (Phase 49)
# =========================================================================


def serialize_build_summary(build: Any) -> dict[str, Any]:
    """
    Serialize a Cloud Build object to a summary dictionary for list_builds.

    Includes status, timing, source info (repo, commit, branch), output images,
    and build trigger ID for correlation.

    Args:
        build: google.cloud.devtools.cloudbuild_v1.Build

    Returns:
        Serialized build summary
    """
    # Extract source info
    source = _extract_build_source(build)

    # Parse timing
    start_time = format_protobuf_timestamp(getattr(build, "start_time", None))
    finish_time = format_protobuf_timestamp(getattr(build, "finish_time", None))
    create_time = format_protobuf_timestamp(getattr(build, "create_time", None))

    # Compute duration
    duration_seconds = None
    raw_start = getattr(build, "start_time", None)
    raw_finish = getattr(build, "finish_time", None)
    if raw_start and raw_finish:
        duration_seconds = format_build_duration(raw_start, raw_finish)

    # Extract output images
    images = _extract_build_images(build)

    # Status: protobuf enum -> string via .name
    status = _safe_enum_name(getattr(build, "status", None))

    return {
        "id": build.id,
        "status": status,
        "status_detail": getattr(build, "status_detail", None) or None,
        "create_time": create_time,
        "start_time": start_time,
        "finish_time": finish_time,
        "duration_seconds": duration_seconds,
        "source": source,
        "build_trigger_id": getattr(build, "build_trigger_id", None) or None,
        "images": images,
        "tags": list(build.tags) if getattr(build, "tags", None) else [],
        "log_url": getattr(build, "log_url", None) or None,
    }


def serialize_build_detail(build: Any) -> dict[str, Any]:
    """
    Serialize a Cloud Build object to a detailed dictionary for get_build.

    Includes all summary fields plus step-level execution, substitutions,
    build options, timeout, and detailed image push timing.

    Args:
        build: google.cloud.devtools.cloudbuild_v1.Build

    Returns:
        Serialized build detail
    """
    # Start with summary fields
    detail = serialize_build_summary(build)

    # Add steps
    steps = []
    for step in getattr(build, "steps", None) or []:
        steps.append(serialize_build_step(step))
    detail["steps"] = steps

    # Substitutions
    subs = getattr(build, "substitutions", None)
    detail["substitutions"] = dict(subs) if subs else {}

    # Build options
    options = getattr(build, "options", None)
    if options:
        detail["options"] = {
            "logging": _safe_enum_name(getattr(options, "logging", None)),
            "machine_type": _safe_enum_name(getattr(options, "machine_type", None)),
        }
    else:
        detail["options"] = None

    # Timeout
    timeout = getattr(build, "timeout", None)
    detail["timeout_seconds"] = (
        timeout.seconds if timeout and getattr(timeout, "seconds", 0) else None
    )

    # Detailed results images with push timing
    results = getattr(build, "results", None)
    results_images = []
    if results and getattr(results, "images", None):
        for img in results.images:
            img_info = {
                "name": getattr(img, "name", None),
                "digest": getattr(img, "digest", None),
            }
            push_timing = getattr(img, "push_timing", None)
            if push_timing:
                img_info["push_timing_start"] = format_protobuf_timestamp(
                    getattr(push_timing, "start_time", None)
                )
                img_info["push_timing_end"] = format_protobuf_timestamp(
                    getattr(push_timing, "end_time", None)
                )
            else:
                img_info["push_timing_start"] = None
                img_info["push_timing_end"] = None
            results_images.append(img_info)
    detail["results_images"] = results_images

    return detail


def serialize_build_step(step: Any) -> dict[str, Any]:
    """
    Serialize a Cloud Build Step to a dictionary.

    Args:
        step: google.cloud.devtools.cloudbuild_v1.BuildStep

    Returns:
        Serialized build step
    """
    # Timing
    timing = getattr(step, "timing", None)
    timing_start = None
    timing_end = None
    duration_seconds = None
    if timing:
        timing_start = format_protobuf_timestamp(getattr(timing, "start_time", None))
        timing_end = format_protobuf_timestamp(getattr(timing, "end_time", None))
        raw_start = getattr(timing, "start_time", None)
        raw_end = getattr(timing, "end_time", None)
        if raw_start and raw_end:
            duration_seconds = format_build_duration(raw_start, raw_end)

    return {
        "name": step.name,
        "id": getattr(step, "id", None) or None,
        "status": _safe_enum_name(getattr(step, "status", None)),
        "timing_start": timing_start,
        "timing_end": timing_end,
        "duration_seconds": duration_seconds,
        "exit_code": getattr(step, "exit_code", None) if hasattr(step, "exit_code") else None,
        "args": list(step.args) if getattr(step, "args", None) else None,
        "dir": step.dir if getattr(step, "dir", None) else None,
        "env": list(step.env) if getattr(step, "env", None) else None,
    }


def serialize_build_trigger(trigger: Any) -> dict[str, Any]:  # NOSONAR (cognitive complexity)
    """
    Serialize a Cloud Build Trigger to a dictionary for list_build_triggers.

    Args:
        trigger: google.cloud.devtools.cloudbuild_v1.BuildTrigger

    Returns:
        Serialized build trigger
    """
    # Trigger template (source repo config)
    trigger_template = None
    tt = getattr(trigger, "trigger_template", None)
    if tt:
        trigger_template = {
            "repo_name": getattr(tt, "repo_name", None) or None,
            "branch_name": getattr(tt, "branch_name", None) or None,
            "tag_name": getattr(tt, "tag_name", None) or None,
        }

    # GitHub config
    github = None
    gh = getattr(trigger, "github", None)
    if gh:
        github = {
            "owner": getattr(gh, "owner", None) or None,
            "name": getattr(gh, "name", None) or None,
        }
        # Push config
        push = getattr(gh, "push", None)
        if push:
            github["push_branch"] = getattr(push, "branch", None) or None
        else:
            github["push_branch"] = None
        # Pull request config
        pr = getattr(gh, "pull_request", None)
        if pr:
            github["pull_request_branch"] = getattr(pr, "branch", None) or None
        else:
            github["pull_request_branch"] = None

    return {
        "id": trigger.id,
        "name": getattr(trigger, "name", None) or None,
        "description": getattr(trigger, "description", None) or None,
        "disabled": getattr(trigger, "disabled", False),
        "create_time": format_protobuf_timestamp(getattr(trigger, "create_time", None)),
        "trigger_template": trigger_template,
        "github": github,
        "filename": getattr(trigger, "filename", None) or None,
        "tags": list(trigger.tags) if getattr(trigger, "tags", None) else [],
    }


# =========================================================================
# CLOUD BUILD SERIALIZER HELPERS (Phase 49)
# =========================================================================


def _extract_build_source(build: Any) -> dict[str, Any]:
    """Extract source information from a Cloud Build object."""
    source_info: dict[str, Any] = {
        "repo_url": None,
        "commit_sha": None,
        "branch": None,
    }

    source = getattr(build, "source", None)
    if not source:
        # Try source_provenance for resolved info
        return _extract_source_provenance(build, source_info)

    # Repo source
    repo_source = getattr(source, "repo_source", None)
    if repo_source:
        source_info["repo_url"] = getattr(repo_source, "repo_name", None) or None
        source_info["branch"] = getattr(repo_source, "branch_name", None) or None
        # Revision could be commit_sha or branch/tag
        revision = getattr(repo_source, "revision", None)
        if revision and not source_info["branch"]:
            # If no branch, revision might be a commit
            source_info["commit_sha"] = revision

    # Storage source (alternative)
    storage_source = getattr(source, "storage_source", None)
    if storage_source and not repo_source:
        source_info["repo_url"] = None
        source_info["commit_sha"] = None

    # Enrich with source_provenance if available
    return _extract_source_provenance(build, source_info)


def _extract_source_provenance(build: Any, source_info: dict[str, Any]) -> dict[str, Any]:
    """Enrich source info with resolved source provenance."""
    provenance = getattr(build, "source_provenance", None)
    if not provenance:
        return source_info

    resolved = getattr(provenance, "resolved_repo_source", None)
    if resolved:
        if not source_info["commit_sha"]:
            revision = getattr(resolved, "revision", None)
            if revision:
                source_info["commit_sha"] = revision
        if not source_info["repo_url"]:
            repo_name = getattr(resolved, "repo_name", None)
            if repo_name:
                source_info["repo_url"] = repo_name

    return source_info


def _extract_build_images(build: Any) -> list[dict[str, str | None]]:
    """Extract output images from a Cloud Build's results."""
    images: list[dict[str, str | None]] = []
    results = getattr(build, "results", None)
    if not results:
        return images

    for img in getattr(results, "images", None) or []:
        images.append(
            {
                "name": getattr(img, "name", None),
                "digest": getattr(img, "digest", None),
            }
        )

    return images


def _safe_enum_name(enum_val: Any) -> str:
    """Safely extract .name from a protobuf enum, returning 'UNKNOWN' on failure."""
    if enum_val is None:
        return "UNKNOWN"
    if hasattr(enum_val, "name"):
        try:
            return str(enum_val.name)
        except Exception:
            return str(enum_val)
    return str(enum_val)


# =========================================================================
# ARTIFACT REGISTRY SERIALIZERS (Phase 49)
# =========================================================================


def serialize_artifact_repository(repo: Any) -> dict[str, Any]:
    """
    Serialize an Artifact Registry Repository to a dictionary.

    Args:
        repo: google.cloud.artifactregistry_v1.Repository

    Returns:
        Serialized repository data
    """
    name = getattr(repo, "name", "") or ""

    # Extract short_name: last segment of the resource path
    # e.g., "projects/x/locations/y/repositories/my-repo" -> "my-repo"
    short_name = name.rsplit("/", 1)[-1] if name else ""

    # Extract location from resource path
    # e.g., "projects/x/locations/us-central1/repositories/my-repo" -> "us-central1"
    location = None
    parts = name.split("/")
    for i, part in enumerate(parts):
        if part == "locations" and i + 1 < len(parts):
            location = parts[i + 1]
            break

    # Format: protobuf uses `format_` (trailing underscore) because
    # "format" is a Python reserved word
    format_val = getattr(repo, "format_", None)
    format_name = format_val.name if format_val and hasattr(format_val, "name") else "UNKNOWN"

    return {
        "name": name,
        "short_name": short_name,
        "format": format_name,
        "description": getattr(repo, "description", None) or None,
        "create_time": format_protobuf_timestamp(getattr(repo, "create_time", None)),
        "update_time": format_protobuf_timestamp(getattr(repo, "update_time", None)),
        "size_bytes": getattr(repo, "size_bytes", None) if hasattr(repo, "size_bytes") else None,
        "location": location,
    }


def serialize_docker_image(image: Any) -> dict[str, Any]:
    """
    Serialize an Artifact Registry DockerImage to a dictionary.

    Includes linkage fields (image_name, tags, digest) for Phase 52
    image-to-build tracing.

    Args:
        image: google.cloud.artifactregistry_v1.DockerImage

    Returns:
        Serialized Docker image data
    """
    uri = getattr(image, "uri", "") or ""

    # Extract image_name: everything after the repository path, before @
    # URI format: "us-central1-docker.pkg.dev/project/repo/image-name@sha256:..."
    image_name = ""
    if uri:
        # Split on @ to separate image path from digest
        path_part = uri.split("@")[0] if "@" in uri else uri
        # The image name is the part after the 3rd / (host/project/repo/image-name)
        segments = path_part.split("/")
        if len(segments) > 3:
            image_name = "/".join(segments[3:])

    # Extract digest from URI (the part after @)
    digest = ""
    if "@" in uri:
        digest = uri.split("@", 1)[1]

    image_size_bytes = getattr(image, "image_size_bytes", 0) or 0

    return {
        "name": getattr(image, "name", None) or None,
        "uri": uri,
        "image_name": image_name,
        "tags": list(image.tags) if getattr(image, "tags", None) else [],
        "digest": digest,
        "image_size_bytes": image_size_bytes,
        "image_size_formatted": format_bytes(image_size_bytes),
        "upload_time": format_protobuf_timestamp(getattr(image, "upload_time", None)),
        "build_time": format_protobuf_timestamp(getattr(image, "build_time", None))
        if hasattr(image, "build_time") and getattr(image, "build_time", None)
        else None,
        "media_type": getattr(image, "media_type", None)
        if getattr(image, "media_type", None)
        else None,
    }
