# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval previews for ``k8s.secret.update`` and ``k8s.secret.delete``.

Both builders read the Secret through the connector, so the approver sees
the real state of the cluster, never a value:

``k8s.secret.update``
    The op is pinned ``credential_write``, so the generic echo of params is
    off and this builder is the only thing the approver sees. It shows the
    Secret, the ``mode``, the key names that will be set, the ``*_secret_ref``
    store paths and mounts, the key names the Secret has now and, in
    ``replace`` mode, the key names that will be removed. Without a connector
    (a unit test, or a preview with no target) it shows the params-derived
    part only.

``k8s.secret.delete``
    The op is ``destructive``, so it cannot wait for approval without a
    ``blast_radius`` block (``object`` / ``children`` / ``irreversibility``).
    ``children`` lists what uses the Secret in the same namespace: Pods, and
    the pod templates of Deployments, StatefulSets, DaemonSets and CronJobs.
    A pod spec uses a Secret through a volume (also a projected volume),
    ``env`` with ``secretKeyRef``, ``envFrom`` with ``secretRef``, or
    ``imagePullSecrets``, in containers and init containers. The values are
    gone after the delete and Kubernetes keeps no copy, so the class is
    ``permanent``. A missing Secret raises, so the preview fails and the
    destructive gate refuses to park the call.

Both builders raise ``KubernetesSecretNotFoundError`` (from
:mod:`ops_secret_write`) for a missing Secret;
:func:`~meho_backplane.operations._preview.build_proposed_effect` turns that
into the ``preview_unavailable`` marker with the message.
"""

from __future__ import annotations

from typing import Any

from kubernetes_asyncio import client

from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
from meho_backplane.connectors.kubernetes.ops_secret_write import (
    read_secret_or_raise,
    secret_key_names,
)
from meho_backplane.connectors.kubernetes.ops_write_preview import _ref_map, _sorted_keys
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

__all__ = ["list_secret_consumers", "secret_uses_in_pod_spec"]

#: ``(kind, api class name, list method, path from the object to its pod spec)``
#: for every workload kind the delete preview scans in the Secret's namespace.
_CONSUMER_KINDS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("pod", "CoreV1Api", "list_namespaced_pod", ("spec",)),
    ("deployment", "AppsV1Api", "list_namespaced_deployment", ("spec", "template", "spec")),
    ("statefulset", "AppsV1Api", "list_namespaced_stateful_set", ("spec", "template", "spec")),
    ("daemonset", "AppsV1Api", "list_namespaced_daemon_set", ("spec", "template", "spec")),
    (
        "cronjob",
        "BatchV1Api",
        "list_namespaced_cron_job",
        ("spec", "job_template", "spec", "template", "spec"),
    ),
)


def _attr_path(obj: Any, path: tuple[str, ...]) -> Any:
    """Follow *path* attribute by attribute; ``None`` as soon as one is missing."""
    for attr in path:
        obj = getattr(obj, attr, None)
        if obj is None:
            return None
    return obj


def _volume_uses(volumes: Any, secret_name: str) -> list[str]:
    uses: list[str] = []
    for volume in volumes or []:
        secret = getattr(volume, "secret", None)
        if secret is not None and getattr(secret, "secret_name", None) == secret_name:
            uses.append("volume")
        projected = getattr(volume, "projected", None)
        for source in getattr(projected, "sources", None) or []:
            projected_secret = getattr(source, "secret", None)
            if getattr(projected_secret, "name", None) == secret_name:
                uses.append("projected_volume")
    return uses


def _container_uses(containers: Any, secret_name: str) -> list[str]:
    uses: list[str] = []
    for container in containers or []:
        for env in getattr(container, "env", None) or []:
            ref = _attr_path(env, ("value_from", "secret_key_ref"))
            if ref is not None and getattr(ref, "name", None) == secret_name:
                uses.append("env")
        for source in getattr(container, "env_from", None) or []:
            ref = getattr(source, "secret_ref", None)
            if ref is not None and getattr(ref, "name", None) == secret_name:
                uses.append("env_from")
    return uses


def secret_uses_in_pod_spec(pod_spec: Any, secret_name: str) -> list[str]:
    """Return how *pod_spec* uses the Secret named *secret_name*; ``[]`` when it does not.

    Each entry is one of ``volume``, ``projected_volume``, ``env``,
    ``env_from`` or ``image_pull_secret``. Containers and init containers
    are both checked. Works on ``kubernetes_asyncio`` model objects.
    """
    if pod_spec is None:
        return []
    uses = _volume_uses(getattr(pod_spec, "volumes", None), secret_name)
    uses += _container_uses(getattr(pod_spec, "containers", None), secret_name)
    uses += _container_uses(getattr(pod_spec, "init_containers", None), secret_name)
    for pull_secret in getattr(pod_spec, "image_pull_secrets", None) or []:
        if getattr(pull_secret, "name", None) == secret_name:
            uses.append("image_pull_secret")
    return sorted(set(uses))


async def list_secret_consumers(
    api_client: Any, *, namespace: str, secret_name: str
) -> list[dict[str, Any]]:
    """List the workloads in *namespace* whose pod spec uses the Secret.

    One row ``{kind, name, uses}`` per Pod, Deployment, StatefulSet,
    DaemonSet or CronJob that references the Secret. Reads metadata and
    specs only, never the Secret.
    """
    consumers: list[dict[str, Any]] = []
    for kind, api_name, list_method, spec_path in _CONSUMER_KINDS:
        api = getattr(client, api_name)(api_client)
        listed = await getattr(api, list_method)(namespace=namespace)
        for item in getattr(listed, "items", None) or []:
            uses = secret_uses_in_pod_spec(_attr_path(item, spec_path), secret_name)
            if uses:
                name = getattr(getattr(item, "metadata", None), "name", None)
                consumers.append({"kind": kind, "name": name, "uses": uses})
    return consumers


def _params_preview(params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The params-derived part of the update preview and the key names it sets."""
    inline_keys = set(_sorted_keys(params.get("string_data")))
    inline_keys |= set(_sorted_keys(params.get("data")))
    string_refs = _ref_map(params.get("string_data_secret_ref"))
    data_refs = _ref_map(params.get("data_secret_ref"))
    keys_set = sorted(inline_keys | set(string_refs or {}) | set(data_refs or {}))
    preview: dict[str, Any] = {
        "resource": "secret",
        "name": params.get("name"),
        "namespace": params.get("namespace"),
        "mode": params.get("mode", "patch"),
        "keys_set": keys_set,
        "inline_key_names": sorted(inline_keys),
    }
    if string_refs is not None:
        preview["string_data_secret_ref"] = string_refs
    if data_refs is not None:
        preview["data_secret_ref"] = data_refs
    for mount_field in ("string_data_secret_mount", "data_secret_mount"):
        mount = params.get(mount_field)
        if mount:
            preview[mount_field] = str(mount)
    return preview, keys_set


async def _read_secret(ctx: PreviewContext) -> Any | None:
    """Read the Secret named in the params; ``None`` without a connector or target."""
    if not isinstance(ctx.connector_instance, KubernetesConnector) or ctx.target is None:
        return None
    api_client = await ctx.connector_instance._get_api_client(ctx.target, ctx.operator)
    core_v1 = client.CoreV1Api(api_client)
    return await read_secret_or_raise(
        core_v1, name=str(ctx.params.get("name")), namespace=str(ctx.params.get("namespace"))
    )


async def _secret_update_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``k8s.secret.update``: the Secret, the key names that change, never a value."""
    preview, keys_set = _params_preview(ctx.params)
    secret = await _read_secret(ctx)
    if secret is None:
        return preview
    current = secret_key_names(secret)
    preview["type"] = secret.type or "Opaque"
    preview["current_key_names"] = current
    preview["immutable"] = bool(getattr(secret, "immutable", None))
    if preview["mode"] == "replace":
        preview["keys_removed"] = sorted(set(current) - set(keys_set))
    return preview


async def _secret_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``k8s.secret.delete``: the Secret and what uses it, as the blast radius.

    Returns ``None`` without a connector or target; the destructive gate
    then refuses to park (fail-closed). A missing Secret raises.
    """
    if not isinstance(ctx.connector_instance, KubernetesConnector) or ctx.target is None:
        return None
    name = str(ctx.params.get("name"))
    namespace = str(ctx.params.get("namespace"))
    api_client = await ctx.connector_instance._get_api_client(ctx.target, ctx.operator)
    secret = await read_secret_or_raise(
        client.CoreV1Api(api_client), name=name, namespace=namespace
    )
    key_names = secret_key_names(secret)
    secret_type = secret.type or "Opaque"
    children = await list_secret_consumers(api_client, namespace=namespace, secret_name=name)
    return {
        "resource": "secret",
        "name": name,
        "namespace": namespace,
        "type": secret_type,
        "key_names": key_names,
        "blast_radius": {
            "object": {
                "kind": "secret",
                "namespace": namespace,
                "name": name,
                "type": secret_type,
                "key_names": key_names,
            },
            "children": children,
            "consumers_checked": [kind for kind, *_ in _CONSUMER_KINDS],
            "irreversibility": "permanent",
            "recovery": "none: the values are gone and Kubernetes keeps no copy",
        },
    }


def _register_kubernetes_secret_write_preview_builders() -> None:
    """Wire the Secret update and delete previews. Called at import time."""
    register_preview_builder("k8s.secret.update", _secret_update_preview)
    register_preview_builder("k8s.secret.delete", _secret_delete_preview)


_register_kubernetes_secret_write_preview_builders()
