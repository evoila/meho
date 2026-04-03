# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Kubernetes Object Serializers

Converts kubernetes-asyncio response objects to dictionaries.
These are used by the handler mixins to return clean data.
"""

import base64
from typing import Any


def serialize_pod(pod: Any) -> dict[str, Any]:  # NOSONAR (cognitive complexity)
    """Serialize a Pod object to a dictionary."""
    metadata = pod.metadata
    spec = pod.spec
    status = pod.status

    # Extract container info
    containers = []
    if spec and spec.containers:
        for c in spec.containers:
            container_status = None
            if status and status.container_statuses:
                for cs in status.container_statuses:
                    if cs.name == c.name:
                        container_status = cs
                        break

            container_info = {
                "name": c.name,
                "image": c.image,
                "ports": (
                    [{"port": p.container_port, "protocol": p.protocol} for p in c.ports]
                    if c.ports
                    else []
                ),
                "resources": {
                    "requests": dict(c.resources.requests)
                    if c.resources and c.resources.requests
                    else {},
                    "limits": dict(c.resources.limits)
                    if c.resources and c.resources.limits
                    else {},
                },
            }

            if container_status:
                container_info["ready"] = container_status.ready
                container_info["restart_count"] = container_status.restart_count
                container_info["started"] = container_status.started

                # State
                if container_status.state:
                    if container_status.state.running:
                        container_info["state"] = "running"
                        container_info["started_at"] = (
                            container_status.state.running.started_at.isoformat()
                            if container_status.state.running.started_at
                            else None
                        )
                    elif container_status.state.waiting:
                        container_info["state"] = "waiting"
                        container_info["waiting_reason"] = container_status.state.waiting.reason
                    elif container_status.state.terminated:
                        container_info["state"] = "terminated"
                        container_info["exit_code"] = container_status.state.terminated.exit_code

            containers.append(container_info)

    # Extract conditions
    conditions = []
    if status and status.conditions:
        for cond in status.conditions:
            conditions.append(
                {
                    "type": cond.type,
                    "status": cond.status,
                    "reason": cond.reason,
                    "message": cond.message,
                    "last_transition_time": (
                        cond.last_transition_time.isoformat() if cond.last_transition_time else None
                    ),
                }
            )

    return {
        "name": metadata.name,
        "namespace": metadata.namespace,
        "uid": metadata.uid,
        "labels": dict(metadata.labels) if metadata.labels else {},
        "annotations": dict(metadata.annotations) if metadata.annotations else {},
        "created": (
            metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
        ),
        "phase": status.phase if status else None,
        "host_ip": status.host_ip if status else None,
        "pod_ip": status.pod_ip if status else None,
        "pod_ips": ([ip.ip for ip in status.pod_ips] if status and status.pod_ips else []),
        "start_time": (status.start_time.isoformat() if status and status.start_time else None),
        "node_name": spec.node_name if spec else None,
        "service_account": spec.service_account_name if spec else None,
        "containers": containers,
        "conditions": conditions,
        "qos_class": status.qos_class if status else None,
    }


def serialize_deployment(deployment: Any) -> dict[str, Any]:  # NOSONAR (cognitive complexity)
    """Serialize a Deployment object to a dictionary."""
    metadata = deployment.metadata
    spec = deployment.spec
    status = deployment.status

    # Extract conditions
    conditions = []
    if status and status.conditions:
        for cond in status.conditions:
            conditions.append(
                {
                    "type": cond.type,
                    "status": cond.status,
                    "reason": cond.reason,
                    "message": cond.message,
                    "last_transition_time": (
                        cond.last_transition_time.isoformat() if cond.last_transition_time else None
                    ),
                }
            )

    return {
        "name": metadata.name,
        "namespace": metadata.namespace,
        "uid": metadata.uid,
        "labels": dict(metadata.labels) if metadata.labels else {},
        "annotations": dict(metadata.annotations) if metadata.annotations else {},
        "created": (
            metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
        ),
        "replicas": spec.replicas if spec else 0,
        "selector": dict(spec.selector.match_labels)
        if spec and spec.selector and spec.selector.match_labels
        else {},
        "strategy": spec.strategy.type if spec and spec.strategy else None,
        "ready_replicas": status.ready_replicas if status else 0,
        "available_replicas": status.available_replicas if status else 0,
        "unavailable_replicas": status.unavailable_replicas if status else 0,
        "updated_replicas": status.updated_replicas if status else 0,
        "observed_generation": status.observed_generation if status else None,
        "conditions": conditions,
    }


def serialize_replicaset(rs: Any) -> dict[str, Any]:
    """Serialize a ReplicaSet object to a dictionary."""
    metadata = rs.metadata
    spec = rs.spec
    status = rs.status

    return {
        "name": metadata.name,
        "namespace": metadata.namespace,
        "uid": metadata.uid,
        "labels": dict(metadata.labels) if metadata.labels else {},
        "created": (
            metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
        ),
        "replicas": spec.replicas if spec else 0,
        "selector": dict(spec.selector.match_labels)
        if spec and spec.selector and spec.selector.match_labels
        else {},
        "ready_replicas": status.ready_replicas if status else 0,
        "available_replicas": status.available_replicas if status else 0,
        "observed_generation": status.observed_generation if status else None,
    }


def serialize_statefulset(sts: Any) -> dict[str, Any]:
    """Serialize a StatefulSet object to a dictionary."""
    metadata = sts.metadata
    spec = sts.spec
    status = sts.status

    return {
        "name": metadata.name,
        "namespace": metadata.namespace,
        "uid": metadata.uid,
        "labels": dict(metadata.labels) if metadata.labels else {},
        "created": (
            metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
        ),
        "replicas": spec.replicas if spec else 0,
        "service_name": spec.service_name if spec else None,
        "selector": dict(spec.selector.match_labels)
        if spec and spec.selector and spec.selector.match_labels
        else {},
        "ready_replicas": status.ready_replicas if status else 0,
        "current_replicas": status.current_replicas if status else 0,
        "updated_replicas": status.updated_replicas if status else 0,
        "current_revision": status.current_revision if status else None,
        "update_revision": status.update_revision if status else None,
    }


def serialize_daemonset(ds: Any) -> dict[str, Any]:
    """Serialize a DaemonSet object to a dictionary."""
    metadata = ds.metadata
    spec = ds.spec
    status = ds.status

    return {
        "name": metadata.name,
        "namespace": metadata.namespace,
        "uid": metadata.uid,
        "labels": dict(metadata.labels) if metadata.labels else {},
        "created": (
            metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
        ),
        "selector": dict(spec.selector.match_labels)
        if spec and spec.selector and spec.selector.match_labels
        else {},
        "desired_number_scheduled": status.desired_number_scheduled if status else 0,
        "current_number_scheduled": status.current_number_scheduled if status else 0,
        "number_ready": status.number_ready if status else 0,
        "number_available": status.number_available if status else 0,
        "number_unavailable": status.number_unavailable if status else 0,
        "updated_number_scheduled": status.updated_number_scheduled if status else 0,
    }


def serialize_job(job: Any) -> dict[str, Any]:  # NOSONAR (cognitive complexity)
    """Serialize a Job object to a dictionary."""
    metadata = job.metadata
    spec = job.spec
    status = job.status

    # Extract conditions
    conditions = []
    if status and status.conditions:
        for cond in status.conditions:
            conditions.append(
                {
                    "type": cond.type,
                    "status": cond.status,
                    "reason": cond.reason,
                    "message": cond.message,
                    "last_transition_time": (
                        cond.last_transition_time.isoformat() if cond.last_transition_time else None
                    ),
                }
            )

    return {
        "name": metadata.name,
        "namespace": metadata.namespace,
        "uid": metadata.uid,
        "labels": dict(metadata.labels) if metadata.labels else {},
        "created": (
            metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
        ),
        "parallelism": spec.parallelism if spec else None,
        "completions": spec.completions if spec else None,
        "backoff_limit": spec.backoff_limit if spec else None,
        "active": status.active if status else 0,
        "succeeded": status.succeeded if status else 0,
        "failed": status.failed if status else 0,
        "start_time": (status.start_time.isoformat() if status and status.start_time else None),
        "completion_time": (
            status.completion_time.isoformat() if status and status.completion_time else None
        ),
        "conditions": conditions,
    }


def serialize_cronjob(cj: Any) -> dict[str, Any]:
    """Serialize a CronJob object to a dictionary."""
    metadata = cj.metadata
    spec = cj.spec
    status = cj.status

    return {
        "name": metadata.name,
        "namespace": metadata.namespace,
        "uid": metadata.uid,
        "labels": dict(metadata.labels) if metadata.labels else {},
        "created": (
            metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
        ),
        "schedule": spec.schedule if spec else None,
        "suspend": spec.suspend if spec else False,
        "concurrency_policy": spec.concurrency_policy if spec else None,
        "successful_jobs_history_limit": spec.successful_jobs_history_limit if spec else None,
        "failed_jobs_history_limit": spec.failed_jobs_history_limit if spec else None,
        "last_schedule_time": (
            status.last_schedule_time.isoformat() if status and status.last_schedule_time else None
        ),
        "last_successful_time": (
            status.last_successful_time.isoformat()
            if status and status.last_successful_time
            else None
        ),
        "active_jobs": len(status.active) if status and status.active else 0,
    }


def serialize_service(svc: Any) -> dict[str, Any]:  # NOSONAR (cognitive complexity)
    """Serialize a Service object to a dictionary."""
    metadata = svc.metadata
    spec = svc.spec
    status = svc.status

    ports = []
    if spec and spec.ports:
        for p in spec.ports:
            ports.append(
                {
                    "name": p.name,
                    "port": p.port,
                    "target_port": str(p.target_port) if p.target_port else None,
                    "protocol": p.protocol,
                    "node_port": p.node_port,
                }
            )

    return {
        "name": metadata.name,
        "namespace": metadata.namespace,
        "uid": metadata.uid,
        "labels": dict(metadata.labels) if metadata.labels else {},
        "annotations": dict(metadata.annotations) if metadata.annotations else {},
        "created": (
            metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
        ),
        "type": spec.type if spec else None,
        "cluster_ip": spec.cluster_ip if spec else None,
        "cluster_ips": spec.cluster_ips if spec else [],
        "external_ips": spec.external_ips if spec else [],
        "external_name": spec.external_name if spec else None,
        "load_balancer_ip": spec.load_balancer_ip if spec else None,
        "ports": ports,
        "selector": dict(spec.selector) if spec and spec.selector else {},
        "session_affinity": spec.session_affinity if spec else None,
        "load_balancer_ingress": (
            [{"ip": ing.ip, "hostname": ing.hostname} for ing in status.load_balancer.ingress]
            if status and status.load_balancer and status.load_balancer.ingress
            else []
        ),
    }


def serialize_ingress(ing: Any) -> dict[str, Any]:  # NOSONAR (cognitive complexity)
    """Serialize an Ingress object to a dictionary."""
    metadata = ing.metadata
    spec = ing.spec
    status = ing.status

    # Extract rules
    rules = []
    if spec and spec.rules:
        for rule in spec.rules:
            paths = []
            if rule.http and rule.http.paths:
                for path in rule.http.paths:
                    paths.append(
                        {
                            "path": path.path,
                            "path_type": path.path_type,
                            "backend": {
                                "service_name": path.backend.service.name
                                if path.backend and path.backend.service
                                else None,
                                "service_port": (
                                    path.backend.service.port.number
                                    if path.backend
                                    and path.backend.service
                                    and path.backend.service.port
                                    else None
                                ),
                            },
                        }
                    )
            rules.append(
                {
                    "host": rule.host,
                    "paths": paths,
                }
            )

    # Extract TLS
    tls = []
    if spec and spec.tls:
        for t in spec.tls:
            tls.append(
                {
                    "hosts": t.hosts if t.hosts else [],
                    "secret_name": t.secret_name,
                }
            )

    return {
        "name": metadata.name,
        "namespace": metadata.namespace,
        "uid": metadata.uid,
        "labels": dict(metadata.labels) if metadata.labels else {},
        "annotations": dict(metadata.annotations) if metadata.annotations else {},
        "created": (
            metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
        ),
        "ingress_class_name": spec.ingress_class_name if spec else None,
        "rules": rules,
        "tls": tls,
        "load_balancer_ingress": (
            [{"ip": ing.ip, "hostname": ing.hostname} for ing in status.load_balancer.ingress]
            if status and status.load_balancer and status.load_balancer.ingress
            else []
        ),
    }


def serialize_endpoints(ep: Any) -> dict[str, Any]:  # NOSONAR (cognitive complexity)
    """Serialize an Endpoints object to a dictionary."""
    metadata = ep.metadata

    subsets = []
    if ep.subsets:
        for subset in ep.subsets:
            addresses = []
            if subset.addresses:
                for addr in subset.addresses:
                    addresses.append(
                        {
                            "ip": addr.ip,
                            "hostname": addr.hostname,
                            "node_name": addr.node_name,
                            "target_ref": (
                                {
                                    "kind": addr.target_ref.kind,
                                    "name": addr.target_ref.name,
                                    "namespace": addr.target_ref.namespace,
                                }
                                if addr.target_ref
                                else None
                            ),
                        }
                    )

            not_ready_addresses = []
            if subset.not_ready_addresses:
                for addr in subset.not_ready_addresses:
                    not_ready_addresses.append(
                        {
                            "ip": addr.ip,
                            "hostname": addr.hostname,
                            "node_name": addr.node_name,
                        }
                    )

            ports = []
            if subset.ports:
                for p in subset.ports:
                    ports.append(
                        {
                            "name": p.name,
                            "port": p.port,
                            "protocol": p.protocol,
                        }
                    )

            subsets.append(
                {
                    "addresses": addresses,
                    "not_ready_addresses": not_ready_addresses,
                    "ports": ports,
                }
            )

    return {
        "name": metadata.name,
        "namespace": metadata.namespace,
        "subsets": subsets,
    }


def serialize_network_policy(np: Any) -> dict[str, Any]:
    """Serialize a NetworkPolicy object to a dictionary."""
    metadata = np.metadata
    spec = np.spec

    return {
        "name": metadata.name,
        "namespace": metadata.namespace,
        "uid": metadata.uid,
        "labels": dict(metadata.labels) if metadata.labels else {},
        "created": (
            metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
        ),
        "pod_selector": (
            dict(spec.pod_selector.match_labels)
            if spec and spec.pod_selector and spec.pod_selector.match_labels
            else {}
        ),
        "policy_types": spec.policy_types if spec else [],
        "ingress_rules_count": len(spec.ingress) if spec and spec.ingress else 0,
        "egress_rules_count": len(spec.egress) if spec and spec.egress else 0,
    }


def serialize_node(node: Any) -> dict[str, Any]:  # NOSONAR (cognitive complexity)
    """Serialize a Node object to a dictionary."""
    metadata = node.metadata
    spec = node.spec
    status = node.status

    # Extract conditions
    conditions = []
    if status and status.conditions:
        for cond in status.conditions:
            conditions.append(
                {
                    "type": cond.type,
                    "status": cond.status,
                    "reason": cond.reason,
                    "message": cond.message,
                    "last_heartbeat_time": (
                        cond.last_heartbeat_time.isoformat() if cond.last_heartbeat_time else None
                    ),
                    "last_transition_time": (
                        cond.last_transition_time.isoformat() if cond.last_transition_time else None
                    ),
                }
            )

    # Extract addresses
    addresses = {}
    if status and status.addresses:
        for addr in status.addresses:
            addresses[addr.type] = addr.address

    return {
        "name": metadata.name,
        "uid": metadata.uid,
        "labels": dict(metadata.labels) if metadata.labels else {},
        "annotations": dict(metadata.annotations) if metadata.annotations else {},
        "created": (
            metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
        ),
        "unschedulable": spec.unschedulable if spec else False,
        "taints": (
            [{"key": t.key, "value": t.value, "effect": t.effect} for t in spec.taints]
            if spec and spec.taints
            else []
        ),
        "addresses": addresses,
        "capacity": dict(status.capacity) if status and status.capacity else {},
        "allocatable": dict(status.allocatable) if status and status.allocatable else {},
        "conditions": conditions,
        "node_info": {
            "machine_id": status.node_info.machine_id if status and status.node_info else None,
            "system_uuid": status.node_info.system_uuid if status and status.node_info else None,
            "boot_id": status.node_info.boot_id if status and status.node_info else None,
            "kernel_version": status.node_info.kernel_version
            if status and status.node_info
            else None,
            "os_image": status.node_info.os_image if status and status.node_info else None,
            "container_runtime_version": (
                status.node_info.container_runtime_version if status and status.node_info else None
            ),
            "kubelet_version": status.node_info.kubelet_version
            if status and status.node_info
            else None,
            "kube_proxy_version": status.node_info.kube_proxy_version
            if status and status.node_info
            else None,
            "operating_system": status.node_info.operating_system
            if status and status.node_info
            else None,
            "architecture": status.node_info.architecture if status and status.node_info else None,
        },
    }


def serialize_namespace(ns: Any) -> dict[str, Any]:
    """Serialize a Namespace object to a dictionary."""
    metadata = ns.metadata
    status = ns.status

    return {
        "name": metadata.name,
        "uid": metadata.uid,
        "labels": dict(metadata.labels) if metadata.labels else {},
        "annotations": dict(metadata.annotations) if metadata.annotations else {},
        "created": (
            metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
        ),
        "phase": status.phase if status else None,
    }


def serialize_configmap(cm: Any) -> dict[str, Any]:
    """Serialize a ConfigMap object to a dictionary."""
    metadata = cm.metadata

    return {
        "name": metadata.name,
        "namespace": metadata.namespace,
        "uid": metadata.uid,
        "labels": dict(metadata.labels) if metadata.labels else {},
        "annotations": dict(metadata.annotations) if metadata.annotations else {},
        "created": (
            metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
        ),
        "data": dict(cm.data) if cm.data else {},
        "binary_data_keys": list(cm.binary_data.keys()) if cm.binary_data else [],
    }


def serialize_secret(secret: Any, decode: bool = False) -> dict[str, Any]:
    """Serialize a Secret object to a dictionary."""
    metadata = secret.metadata

    data = {}
    if secret.data:
        for key, value in secret.data.items():
            if decode:
                try:
                    data[key] = base64.b64decode(value).decode("utf-8")
                except Exception:
                    data[key] = "<binary data>"
            else:
                data[key] = value  # Keep base64 encoded

    return {
        "name": metadata.name,
        "namespace": metadata.namespace,
        "uid": metadata.uid,
        "type": secret.type,
        "labels": dict(metadata.labels) if metadata.labels else {},
        "annotations": dict(metadata.annotations) if metadata.annotations else {},
        "created": (
            metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
        ),
        "data": data,
    }


def serialize_pvc(pvc: Any) -> dict[str, Any]:
    """Serialize a PersistentVolumeClaim object to a dictionary."""
    metadata = pvc.metadata
    spec = pvc.spec
    status = pvc.status

    return {
        "name": metadata.name,
        "namespace": metadata.namespace,
        "uid": metadata.uid,
        "labels": dict(metadata.labels) if metadata.labels else {},
        "created": (
            metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
        ),
        "access_modes": spec.access_modes if spec else [],
        "storage_class_name": spec.storage_class_name if spec else None,
        "volume_mode": spec.volume_mode if spec else None,
        "volume_name": spec.volume_name if spec else None,
        "resources": {
            "requests": dict(spec.resources.requests)
            if spec and spec.resources and spec.resources.requests
            else {},
        },
        "phase": status.phase if status else None,
        "capacity": dict(status.capacity) if status and status.capacity else {},
    }


def serialize_pv(pv: Any) -> dict[str, Any]:
    """Serialize a PersistentVolume object to a dictionary."""
    metadata = pv.metadata
    spec = pv.spec
    status = pv.status

    return {
        "name": metadata.name,
        "uid": metadata.uid,
        "labels": dict(metadata.labels) if metadata.labels else {},
        "created": (
            metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
        ),
        "capacity": dict(spec.capacity) if spec and spec.capacity else {},
        "access_modes": spec.access_modes if spec else [],
        "reclaim_policy": spec.persistent_volume_reclaim_policy if spec else None,
        "storage_class_name": spec.storage_class_name if spec else None,
        "volume_mode": spec.volume_mode if spec else None,
        "phase": status.phase if status else None,
        "claim_ref": (
            {
                "namespace": spec.claim_ref.namespace,
                "name": spec.claim_ref.name,
            }
            if spec and spec.claim_ref
            else None
        ),
    }


def serialize_storageclass(sc: Any) -> dict[str, Any]:
    """Serialize a StorageClass object to a dictionary."""
    metadata = sc.metadata

    return {
        "name": metadata.name,
        "uid": metadata.uid,
        "labels": dict(metadata.labels) if metadata.labels else {},
        "annotations": dict(metadata.annotations) if metadata.annotations else {},
        "created": (
            metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
        ),
        "provisioner": sc.provisioner,
        "reclaim_policy": sc.reclaim_policy,
        "volume_binding_mode": sc.volume_binding_mode,
        "allow_volume_expansion": sc.allow_volume_expansion,
        "parameters": dict(sc.parameters) if sc.parameters else {},
    }


def serialize_event(event: Any) -> dict[str, Any]:
    """Serialize an Event object to a dictionary."""
    metadata = event.metadata

    return {
        "name": metadata.name,
        "namespace": metadata.namespace,
        "uid": metadata.uid,
        "type": event.type,
        "reason": event.reason,
        "message": event.message,
        "count": event.count,
        "first_timestamp": (event.first_timestamp.isoformat() if event.first_timestamp else None),
        "last_timestamp": (event.last_timestamp.isoformat() if event.last_timestamp else None),
        "involved_object": {
            "kind": event.involved_object.kind if event.involved_object else None,
            "name": event.involved_object.name if event.involved_object else None,
            "namespace": event.involved_object.namespace if event.involved_object else None,
            "uid": event.involved_object.uid if event.involved_object else None,
        },
        "source": {
            "component": event.source.component if event.source else None,
            "host": event.source.host if event.source else None,
        },
    }
