# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval previews for ``k8s.secret.create``, ``k8s.job.create`` (#4850)
and ``k8s.cronjob.run``.

The first two ops are pinned ``credential_write``
(:data:`~meho_backplane.broadcast.events._CREDENTIAL_WRITE_OPS`): a Secret's
values ride in the params, and a Job's pod template can hold secrets in its
``env`` values or its command line. For such an op the generic echo of
params is turned off, so without these builders the approver saw only the op
id and the target. ``k8s.cronjob.run`` takes only names, but its params say
nothing about what will run: the builder reads the CronJob and shows it.

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
``k8s.cronjob.run``    ``{cronjob, namespace, job_name, schedule, suspend,
                       concurrency_policy, active_jobs, containers,
                       init_containers}``; containers as above.
                       ``active_jobs`` names this CronJob's Jobs that
                       still run. Read from the cluster at park time.
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

``k8s.apply`` has its own builder in :mod:`meho_backplane.operations._preview`
(a server dry-run). It uses :func:`apply_manifest_echo` and
:func:`apply_subject` from this module to also show the manifest it applies
(#3877). The echo hides the same kind of values:

* A Secret document's ``data`` and ``stringData`` values: only the key names
  stay.
* Every ``env`` entry's ``value``: only the variable name stays.
* The ``kubectl.kubernetes.io/last-applied-configuration`` annotation: it is
  a full copy of an earlier object and can hold all of the above.

The Secret and Job builders read only ``ctx.params`` and do no network
calls. The CronJob builder reads the CronJob once; a missing CronJob raises,
and the park then stores the ``preview_unavailable`` marker with the reason.
"""

from __future__ import annotations

import json
from typing import Any

from kubernetes_asyncio import client

from meho_backplane.connectors.kubernetes.ops_write_dangerous import _parse_manifest
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

#: Put in place of a value the approver must not see.
_HIDDEN = "***REDACTED***"

#: This annotation holds a full JSON copy of an earlier object, Secret values included.
_LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"

#: Most bytes of manifest the ``k8s.apply`` echo keeps. A document that does
#: not fit is shown by its identity only, so a huge manifest cannot fill the
#: approval row.
MANIFEST_ECHO_MAX_BYTES = 16_384

#: Most objects named in the ``k8s.apply`` subject line.
_SUBJECT_MAX_OBJECTS = 5


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


async def _cronjob_run_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``k8s.cronjob.run`` — what the CronJob's template runs, never a value.

    Reads the CronJob through the connector. Declines (``None``) when no
    Kubernetes connector or target resolved, so the generic echo applies.
    """
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.ops_cronjob import model_to_json, read_cronjob

    connector = ctx.connector_instance
    if not isinstance(connector, KubernetesConnector) or ctx.target is None:
        return None
    cronjob_name = str(ctx.params.get("cronjob") or "")
    namespace = str(ctx.params.get("namespace") or "")
    api_client = await connector._get_api_client(ctx.target, ctx.operator)
    cronjob = await read_cronjob(client.BatchV1Api(api_client), namespace, cronjob_name)
    spec = cronjob.spec
    pod_spec = model_to_json(spec.job_template.spec.template.spec)
    if not isinstance(pod_spec, dict):
        pod_spec = {}
    active = cronjob.status.active if cronjob.status is not None else None
    return {
        "resource": "cronjob_run",
        "cronjob": cronjob_name,
        "namespace": namespace,
        "job_name": ctx.params.get("job_name") or f"{cronjob_name}-manual-<random>",
        "schedule": spec.schedule,
        "suspend": bool(spec.suspend),
        "concurrency_policy": spec.concurrency_policy,
        "active_jobs": [ref.name for ref in active or [] if ref.name],
        "containers": _containers(pod_spec, "containers"),
        "init_containers": _containers(pod_spec, "initContainers"),
    }


def _hide_env_values(node: Any) -> Any:
    """Copy *node*, hiding the ``value`` of every entry in an ``env`` list."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, item in node.items():
            if key == "env" and isinstance(item, list):
                out[key] = [
                    {**entry, "value": _HIDDEN}
                    if isinstance(entry, dict) and "value" in entry
                    else _hide_env_values(entry)
                    for entry in item
                ]
            else:
                out[key] = _hide_env_values(item)
        return out
    if isinstance(node, list):
        return [_hide_env_values(item) for item in node]
    return node


def _hide_document_secrets(doc: dict[str, Any]) -> dict[str, Any]:
    """Copy one manifest document with the values that can hold a secret hidden."""
    hidden: dict[str, Any] = _hide_env_values(doc)
    if hidden.get("kind") == "Secret":
        for field in ("data", "stringData"):
            if field in hidden:
                value = hidden[field]
                hidden[field] = (
                    {str(key): _HIDDEN for key in value} if isinstance(value, dict) else _HIDDEN
                )
    metadata = hidden.get("metadata")
    annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
    if isinstance(annotations, dict) and _LAST_APPLIED in annotations:
        annotations[_LAST_APPLIED] = _HIDDEN
    return hidden


def _identity_only(doc: dict[str, Any], size: int) -> dict[str, Any]:
    """A document cut down to its identity, saying why the rest is left out."""
    raw_metadata = doc.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    identity = {key: metadata[key] for key in ("name", "namespace") if key in metadata}
    return {
        "apiVersion": doc.get("apiVersion"),
        "kind": doc.get("kind"),
        "metadata": identity,
        "echo_omitted": f"document is {size} bytes; the echo keeps at most "
        f"{MANIFEST_ECHO_MAX_BYTES} bytes of manifest",
    }


def apply_manifest_echo(manifest: str) -> list[dict[str, Any]]:
    """The ``k8s.apply`` manifest as one dict per document, safe to show.

    Secret ``data`` / ``stringData`` values, ``env`` values and the
    last-applied annotation are hidden (see the module docstring). The
    documents are kept in order until :data:`MANIFEST_ECHO_MAX_BYTES` is
    used up; each document after that keeps only its identity. The caller
    still runs the shared params redaction over the result.
    """
    echo: list[dict[str, Any]] = []
    used = 0
    for doc in _parse_manifest(manifest):
        hidden = _hide_document_secrets(doc)
        size = len(json.dumps(hidden, default=str).encode("utf-8"))
        if used + size > MANIFEST_ECHO_MAX_BYTES:
            echo.append(_identity_only(hidden, size))
            continue
        used += size
        echo.append(hidden)
    return echo


def apply_subject(applied: Any) -> str | None:
    """Name the objects a ``k8s.apply`` touches, e.g. ``Deployment/prod/web``.

    Built from the dry-run's per-document summary (``kind``, ``namespace``,
    ``name``). A cluster-wide object has no namespace part. Names at most
    five objects and counts the rest. ``None`` when nothing is named.
    """
    if not isinstance(applied, list):
        return None
    names = [
        "/".join(str(entry[key]) for key in ("kind", "namespace", "name") if entry.get(key))
        for entry in applied
        if isinstance(entry, dict) and entry.get("kind") and entry.get("name")
    ]
    if not names:
        return None
    shown = ", ".join(names[:_SUBJECT_MAX_OBJECTS])
    rest = len(names) - _SUBJECT_MAX_OBJECTS
    return f"{shown} and {rest} more" if rest > 0 else shown


def _register_kubernetes_write_preview_builders() -> None:
    """Wire the Secret, Job create and CronJob run previews. Called at import time."""
    register_preview_builder("k8s.secret.create", _secret_create_preview)
    register_preview_builder("k8s.job.create", _job_create_preview)
    register_preview_builder("k8s.cronjob.run", _cronjob_run_preview)


_register_kubernetes_write_preview_builders()
