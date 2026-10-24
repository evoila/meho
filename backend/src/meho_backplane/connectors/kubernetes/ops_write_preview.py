# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval previews for ``k8s.secret.create`` and ``k8s.job.create`` (#4850).

Both ops are pinned ``credential_write``
(:data:`~meho_backplane.broadcast.events._CREDENTIAL_WRITE_OPS`): a Secret's
values ride in the params, and a Job's pod template can hold secrets in its
``env`` values or its command line. For such an op the generic echo of
params is turned off, so without these builders the approver saw only the op
id and the target.

=====================  ========================================================
op_id                  preview
=====================  ========================================================
``k8s.secret.create``  ``{name, namespace, type, key_names, inline_key_names,
                       string_data_secret_ref?, data_secret_ref?,
                       string_data_secret_mount?, data_secret_mount?}``
``k8s.job.create``     ``{name, namespace, service_account_name?,
                       containers, init_containers}``; each container
                       is ``{name, image, env_var_names, env_from,
                       command_bytes?}``
=====================  ========================================================

What is never shown
===================

* A Secret value: only the key names. A ``*_secret_ref`` map is shown,
  because it holds a store path per key (``vault:<path>#<field>``), not a
  value. The value is read from the store only when the approved call runs.
  The ``*_secret_mount`` names are shown too: they say which store mount the
  paths are read from.
* A Job ``env`` value: only the variable names. ``env_from`` shows the names
  of the Secrets and ConfigMaps the pod loads, not their content.
* A Job ``command`` / ``args``: only their size in bytes, because a command
  line can hold a token (the guest ``program.run`` preview does the same).

The builders read only ``ctx.params`` and do no network calls.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.operations._preview import PreviewContext, register_preview_builder


def _sorted_keys(value: Any) -> list[str]:
    """Sorted key names of a dict param; ``[]`` for anything else."""
    return sorted(str(key) for key in value) if isinstance(value, dict) else []


def _ref_map(value: Any) -> dict[str, str] | None:
    """A ``{secret-key: store-ref}`` map as plain strings, or ``None`` when absent."""
    if not isinstance(value, dict) or not value:
        return None
    return {str(key): str(ref) for key, ref in sorted(value.items())}


async def _secret_create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``k8s.secret.create`` — the Secret and its key names, never a value."""
    params = ctx.params
    inline_keys = set(_sorted_keys(params.get("string_data"))) | set(
        _sorted_keys(params.get("data"))
    )
    string_refs = _ref_map(params.get("string_data_secret_ref"))
    data_refs = _ref_map(params.get("data_secret_ref"))
    all_keys = inline_keys | set(string_refs or {}) | set(data_refs or {})
    preview: dict[str, Any] = {
        "resource": "secret",
        "name": params.get("name"),
        "namespace": params.get("namespace"),
        "type": params.get("type", "Opaque"),
        "key_names": sorted(all_keys),
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
    return preview


def _env_from_names(container: dict[str, Any]) -> list[str]:
    """``secret:<name>`` / ``config_map:<name>`` for each ``envFrom`` source."""
    names: list[str] = []
    for source in container.get("envFrom") or []:
        if not isinstance(source, dict):
            continue
        for field, kind in (("secretRef", "secret"), ("configMapRef", "config_map")):
            ref = source.get(field)
            if isinstance(ref, dict) and ref.get("name"):
                names.append(f"{kind}:{ref['name']}")
    return names


def _command_bytes(container: dict[str, Any]) -> int:
    """Total UTF-8 size of ``command`` + ``args``; the text itself is withheld."""
    parts = [*(container.get("command") or []), *(container.get("args") or [])]
    return sum(len(str(part).encode("utf-8")) for part in parts)


def _container_summary(container: Any) -> dict[str, Any] | None:
    """Name, image, env var names and env sources of one container; no values."""
    if not isinstance(container, dict):
        return None
    summary: dict[str, Any] = {
        "name": container.get("name"),
        "image": container.get("image"),
        "env_var_names": sorted(
            str(entry["name"])
            for entry in container.get("env") or []
            if isinstance(entry, dict) and "name" in entry
        ),
        "env_from": _env_from_names(container),
    }
    size = _command_bytes(container)
    if size:
        summary["command_bytes"] = size
    return summary


def _containers(pod_spec: dict[str, Any], field: str) -> list[dict[str, Any]]:
    """Summaries of the containers listed under *field* of the pod spec."""
    raw = pod_spec.get(field)
    if not isinstance(raw, list):
        return []
    return [s for s in (_container_summary(c) for c in raw) if s is not None]


async def _job_create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``k8s.job.create`` — the Job and what its pods run, never a value."""
    spec = ctx.params.get("spec")
    template = spec.get("template") if isinstance(spec, dict) else None
    pod_spec = template.get("spec") if isinstance(template, dict) else None
    if not isinstance(pod_spec, dict):
        pod_spec = {}
    preview: dict[str, Any] = {
        "resource": "job",
        "name": ctx.params.get("name"),
        "namespace": ctx.params.get("namespace"),
        "containers": _containers(pod_spec, "containers"),
        "init_containers": _containers(pod_spec, "initContainers"),
    }
    service_account = pod_spec.get("serviceAccountName")
    if isinstance(service_account, str) and service_account:
        preview["service_account_name"] = service_account
    return preview


def _register_kubernetes_write_preview_builders() -> None:
    """Wire the Secret and Job create previews. Called at import time."""
    register_preview_builder("k8s.secret.create", _secret_create_preview)
    register_preview_builder("k8s.job.create", _job_create_preview)


_register_kubernetes_write_preview_builders()
