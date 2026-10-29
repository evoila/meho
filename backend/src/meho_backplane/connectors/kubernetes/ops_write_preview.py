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

* A Secret's ``data`` and ``stringData`` values: only the key names stay.
  This also applies to a Secret nested inside another document, for
  example a custom resource that carries a Secret in its spec.
* Every ``env`` entry's ``value``: only the variable name stays.
* Every ``httpHeaders`` entry's ``value`` in a probe or lifecycle hook
  ``httpGet``: only the header name stays.
* Every ``command`` and ``args`` list, at any depth (containers, init and
  ephemeral containers, probe and lifecycle ``exec``): only its size in
  bytes stays, as ``{"hidden": true, "bytes": N}``, for the same reason as
  the Job preview above.
* The ``kubectl.kubernetes.io/last-applied-configuration`` annotation, in
  any ``annotations`` map at any depth (the document's own metadata, a pod
  template's metadata, a nested object): it is a full copy of an earlier
  object and can hold all of the above.

The Secret and Job builders read only ``ctx.params`` and do no network
calls. The CronJob builder reads the CronJob once; a missing CronJob raises,
and the park then stores the ``preview_unavailable`` marker with the reason.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from kubernetes_asyncio import client

from meho_backplane.connectors.kubernetes.ops_write_dangerous import _parse_manifest
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

#: Put in place of a value the approver must not see.
_HIDDEN = "***REDACTED***"

#: This annotation holds a full JSON copy of an earlier object, Secret values included.
_LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"

#: Most bytes of manifest the ``k8s.apply`` echo keeps, counting every entry
#: of the echo as it is stored, after every redaction. A document that does
#: not fit in full is shown by its identity only; when not even that fits,
#: the rest of the documents are summed up in one closing entry. So a huge
#: manifest, or a huge number of documents, cannot fill the approval row.
MANIFEST_ECHO_MAX_BYTES = 16_384

#: The shared params redaction the caller runs over the hidden documents,
#: before the echo budget is applied. Takes and returns one dict per document.
ManifestRedactor = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]

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


def _parts_bytes(parts: list[Any]) -> int:
    """Total UTF-8 size of the parts of a command line."""
    return sum(len(str(part).encode("utf-8")) for part in parts)


def _command_bytes(container: dict[str, Any]) -> int:
    """Total UTF-8 size of ``command`` + ``args``; the text itself is withheld."""
    return _parts_bytes([*(container.get("command") or []), *(container.get("args") or [])])


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


#: Lists of ``{name, value}`` entries whose ``value`` is hidden: container
#: ``env`` and the ``httpHeaders`` of a probe or lifecycle ``httpGet``.
_NAME_VALUE_LISTS = ("env", "httpHeaders")

#: Command-line lists shown only by their size: a container's ``command`` and
#: ``args``, and the ``exec.command`` of a probe or lifecycle hook.
_COMMAND_LISTS = ("command", "args")


#: Fields of a Secret whose values are hidden; the key names stay.
_SECRET_VALUE_FIELDS = ("data", "stringData")


def _hide_secret_values(secret: dict[str, Any]) -> dict[str, Any]:
    """Copy a ``kind: Secret`` dict with every ``data`` / ``stringData`` value hidden."""
    out = dict(secret)
    for field in _SECRET_VALUE_FIELDS:
        if field in out:
            value = out[field]
            out[field] = (
                {str(key): _HIDDEN for key in value} if isinstance(value, dict) else _HIDDEN
            )
    return out


def _hide_values(node: Any) -> Any:
    """Copy *node*, hiding at any depth every value that can hold a secret.

    * The ``value`` of every entry in an ``env`` or ``httpHeaders`` list is
      hidden; the name stays. The entry's other fields are still walked, so
      a Secret or a command line nested in such an entry is hidden too.
    * Every ``command`` or ``args`` list becomes ``{"hidden": true, "bytes":
      <size>}``, like the ``k8s.job.create`` preview's ``command_bytes``.
    * Every dict with ``kind: Secret`` keeps only the key names of its
      ``data`` and ``stringData``. This catches a Secret nested in another
      object, not only a top-level Secret document.
    * The last-applied annotation is hidden in every ``annotations`` map,
      so a pod template's copy is caught too.
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, item in node.items():
            if key in _NAME_VALUE_LISTS and isinstance(item, list):
                out[key] = [
                    _hide_values({**entry, "value": _HIDDEN})
                    if isinstance(entry, dict) and "value" in entry
                    else _hide_values(entry)
                    for entry in item
                ]
            elif key in _COMMAND_LISTS and isinstance(item, list):
                out[key] = {"hidden": True, "bytes": _parts_bytes(item)}
            elif key == "annotations" and isinstance(item, dict) and _LAST_APPLIED in item:
                out[key] = _hide_values({**item, _LAST_APPLIED: _HIDDEN})
            else:
                out[key] = _hide_values(item)
        return _hide_secret_values(out) if out.get("kind") == "Secret" else out
    if isinstance(node, list):
        return [_hide_values(item) for item in node]
    return node


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


def _omitted_summary(count: int) -> dict[str, Any]:
    """The closing entry that counts the documents the echo has no room for."""
    return {
        "omitted_documents": count,
        "echo_omitted": f"{count} more document(s) not shown; the echo keeps at most "
        f"{MANIFEST_ECHO_MAX_BYTES} bytes of manifest",
    }


def _entry_bytes(entry: dict[str, Any]) -> int:
    """UTF-8 size of one echo entry as JSON, plus the two bytes it costs in the list.

    Summed over the entries, this is the size of the whole echo as JSON:
    each entry adds a ``, `` separator, and the ``[`` and ``]`` of the list
    take the place of the separator the last entry does not need.
    """
    return len(json.dumps(entry, default=str).encode("utf-8")) + 2


def apply_manifest_echo(manifest: str, *, redact: ManifestRedactor) -> list[dict[str, Any]]:
    """The ``k8s.apply`` manifest as one dict per document, safe to show.

    Secret ``data`` / ``stringData`` values, ``env`` and ``httpHeaders``
    values, ``command`` / ``args`` text and the last-applied annotation are
    hidden at any depth (see the module docstring). Then *redact*, the
    caller's shared params redaction, runs over all documents. Only then is
    the budget applied, because a redaction marker can be longer than the
    value it replaces: the stored echo is what is measured. The documents
    are kept in order, and every entry of the result counts toward
    :data:`MANIFEST_ECHO_MAX_BYTES`: a document that does not fit in full
    keeps only its identity, and once not even an identity fits, one last
    entry counts the documents left out.
    """
    docs = redact([_hide_values(doc) for doc in _parse_manifest(manifest)])
    # Room is kept for the closing summary while documents may still follow,
    # so the whole echo stays within the budget, summary included.
    summary_bytes = _entry_bytes(_omitted_summary(len(docs)))
    echo: list[dict[str, Any]] = []
    used = 0
    for index, hidden in enumerate(docs):
        budget = MANIFEST_ECHO_MAX_BYTES - (summary_bytes if index < len(docs) - 1 else 0)
        size = _entry_bytes(hidden)
        if used + size <= budget:
            echo.append(hidden)
            used += size
            continue
        identity = _identity_only(hidden, size)
        identity_size = _entry_bytes(identity)
        if used + identity_size <= budget:
            echo.append(identity)
            used += identity_size
            continue
        echo.append(_omitted_summary(len(docs) - index))
        break
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
