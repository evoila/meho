# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The per-object summary on the approval card of ``k8s.apply``.

A person who approves an apply reads the manifest and the dry run. For a
manifest with many objects that is a lot, and the result depends on what is
already on the cluster. This module builds a short summary for each object,
so the person can decide faster. It is information only: it changes no
approval rule, and nothing runs without approval.

For each object the summary holds:

* ``api_group``, ``kind``, ``namespace`` and ``name``. A group of ``core``
  is the group of the old ``v1`` kinds.
* ``state``: ``new`` or ``exists``, from one read of the live object. A
  failed read gives ``unknown`` and a ``state_note`` that says why. It is
  never shown as ``new``. MEHO reads the live object only for a login with
  the role Operator or higher. For any other login there is no read: the
  state is ``not_read`` and the ``state_note`` says why.
* ``other_writers``: who else owns fields of the live object (its
  ``managedFields``), without the ``meho`` manager. ``None`` when the live
  object was not read or could not be read.
* ``risky_fields``: what is risky in the object as it would be after the
  apply (the object that the dry run returns). Only the places that
  Kubernetes defines are read (:data:`_PLACES`): the pod spec of each
  built-in kind that holds one, and the other fields that Kubernetes
  defines as a reference. A Secret object is named, never read. ``None``
  when the dry run returned no object.
* ``built_in``: ``True`` only for a kind that Kubernetes itself defines in
  that API group (:data:`_BUILT_IN_KINDS`). The group and the kind are
  always read together.
* ``cluster_wide``: ``True`` for a kind that is not in a namespace.

The summary shows names, and a name that does not pass its check is not
shown. On purpose it also shows three things that are not names, because
the person needs them to decide: the path of a ``hostPath`` volume, a host
port number, and the manager names of the other writers of the live
object. A manager name passes its own check (:func:`_shown_manager`). The
summary never shows a Secret value, an ``env`` value or a command line.

MEHO reads the whole live object once, with the target's login, and uses
only its ``managedFields``. Nothing else of it is kept or shown.

The builder of the approval card turns the summary on by setting
:data:`apply_summaries_var`. Only a person's card does this: the preview an
agent reads has no summary, and it makes no extra read.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import structlog

__all__ = [
    "NOT_READ_TEXT",
    "OBJECT_SUMMARY_KEY",
    "ApplySummaries",
    "apply_summaries_var",
    "summarize_applied_object",
]

_log = structlog.get_logger(__name__)

#: The key of the ``k8s.apply`` preview that holds the summary list. Only a
#: person reads it.
OBJECT_SUMMARY_KEY = "object_summary"

#: Why an entry has no live state: the login that asked may not run an apply.
NOT_READ_TEXT = "The live object was not read: the login that asked has a role below Operator."

#: The text shown in place of a value that does not pass its check.
_NAME_NOT_SHOWN = "(name not shown)"
_NOT_SHOWN = "(not shown)"


@dataclass
class ApplySummaries:
    """The summaries of one card: one entry per object whose dry run worked.

    ``read_live`` is ``True`` only when the login that asked may run an
    apply; else no live object is read.
    """

    read_live: bool
    entries: list[dict[str, Any]] = field(default_factory=list)


#: Set by the builder of the approval card. ``_apply_one`` adds the summary
#: of each object of a dry run to it. ``None`` (the default): no summary and
#: no extra read.
apply_summaries_var: ContextVar[ApplySummaries | None] = ContextVar(
    "k8s_apply_summaries", default=None
)

#: The kinds that Kubernetes itself defines, by API group (``""`` is the
#: core group). Any other group and kind is not a built-in kind.
_BUILT_IN_KINDS: dict[str, frozenset[str]] = {
    "": frozenset(
        {
            "Binding",
            "ComponentStatus",
            "ConfigMap",
            "Endpoints",
            "Event",
            "LimitRange",
            "Namespace",
            "Node",
            "PersistentVolume",
            "PersistentVolumeClaim",
            "Pod",
            "PodTemplate",
            "ReplicationController",
            "ResourceQuota",
            "Secret",
            "Service",
            "ServiceAccount",
        }
    ),
    "admissionregistration.k8s.io": frozenset(
        {
            "MutatingAdmissionPolicy",
            "MutatingAdmissionPolicyBinding",
            "MutatingWebhookConfiguration",
            "ValidatingAdmissionPolicy",
            "ValidatingAdmissionPolicyBinding",
            "ValidatingWebhookConfiguration",
        }
    ),
    "apiextensions.k8s.io": frozenset({"CustomResourceDefinition"}),
    "apiregistration.k8s.io": frozenset({"APIService"}),
    "apps": frozenset(
        {"ControllerRevision", "DaemonSet", "Deployment", "ReplicaSet", "StatefulSet"}
    ),
    "authentication.k8s.io": frozenset({"SelfSubjectReview", "TokenReview"}),
    "authorization.k8s.io": frozenset(
        {
            "LocalSubjectAccessReview",
            "SelfSubjectAccessReview",
            "SelfSubjectRulesReview",
            "SubjectAccessReview",
        }
    ),
    "autoscaling": frozenset({"HorizontalPodAutoscaler"}),
    "batch": frozenset({"CronJob", "Job"}),
    "certificates.k8s.io": frozenset(
        {"CertificateSigningRequest", "ClusterTrustBundle", "PodCertificateRequest"}
    ),
    "coordination.k8s.io": frozenset({"Lease", "LeaseCandidate"}),
    "discovery.k8s.io": frozenset({"EndpointSlice"}),
    "events.k8s.io": frozenset({"Event"}),
    "flowcontrol.apiserver.k8s.io": frozenset({"FlowSchema", "PriorityLevelConfiguration"}),
    "internal.apiserver.k8s.io": frozenset({"StorageVersion"}),
    "networking.k8s.io": frozenset(
        {"IPAddress", "Ingress", "IngressClass", "NetworkPolicy", "ServiceCIDR"}
    ),
    "node.k8s.io": frozenset({"RuntimeClass"}),
    "policy": frozenset({"Eviction", "PodDisruptionBudget"}),
    "rbac.authorization.k8s.io": frozenset(
        {"ClusterRole", "ClusterRoleBinding", "Role", "RoleBinding"}
    ),
    "resource.k8s.io": frozenset(
        {
            "DeviceClass",
            "DeviceTaintRule",
            "ResourceClaim",
            "ResourceClaimTemplate",
            "ResourcePoolStatusRequest",
            "ResourceSlice",
        }
    ),
    "scheduling.k8s.io": frozenset({"PodGroup", "PriorityClass", "Workload"}),
    "storage.k8s.io": frozenset(
        {
            "CSIDriver",
            "CSINode",
            "CSIStorageCapacity",
            "StorageClass",
            "VolumeAttachment",
            "VolumeAttributesClass",
        }
    ),
    "storagemigration.k8s.io": frozenset({"StorageVersionMigration"}),
}

_RBAC_GROUP = "rbac.authorization.k8s.io"
_RBAC_ROLES = frozenset({"Role", "ClusterRole"})
_HOST_FLAGS = ("hostNetwork", "hostPID", "hostIPC")
_OUTSIDE_SERVICE_TYPES = frozenset({"NodePort", "LoadBalancer"})
_CONTAINER_LISTS = ("initContainers", "containers", "ephemeralContainers")
#: The volume sources whose ``secretRef`` names a Secret.
_SECRET_REF_SOURCES = ("cephfs", "cinder", "flexVolume", "iscsi", "rbd", "scaleIO", "storageos")
#: The Secret references of a CSI volume source.
_CSI_SECRET_REFS = (
    "nodePublishSecretRef",
    "nodeStageSecretRef",
    "nodeExpandSecretRef",
    "controllerPublishSecretRef",
    "controllerExpandSecretRef",
)
#: The two values Kubernetes defines for the ``operation`` of a manager.
_WRITER_OPERATIONS = frozenset({"Apply", "Update"})

#: Most risky fields and other writers named for one object, and the longest
#: text of one entry.
_MAX_ITEMS = 20
_MAX_TEXT = 200

#: A Kubernetes object name. Anything else is not shown as a name.
_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
#: A manager name of ``managedFields``: letters, digits, ``.``, ``-``, ``_``
#: and ``/``, at most 63 characters.
_MANAGER_RE = re.compile(r"^[A-Za-z0-9._/-]{1,63}$")


def _group_of(api_version: str) -> str:
    """The API group of an ``apiVersion``: ``apps`` of ``apps/v1``, ``""`` of ``v1``."""
    return api_version.rpartition("/")[0]


def _shown_name(value: Any) -> str:
    """*value* when it is an object name, else a fixed text. Never an arbitrary string."""
    if isinstance(value, str) and _NAME_RE.match(value):
        return value
    return _NAME_NOT_SHOWN


def _shown_role_name(value: Any) -> str:
    """A role name: an object name, or ``system:`` names whose parts each are one."""
    if isinstance(value, str) and value.startswith("system:"):
        parts = value.split(":")
        return value if all(_NAME_RE.match(part) for part in parts) else _NAME_NOT_SHOWN
    return _shown_name(value)


def _shown_manager(value: str) -> str:
    """A manager name of ``managedFields`` when it passes its check, else a fixed text."""
    return value if _MANAGER_RE.match(value) else _NAME_NOT_SHOWN


def _short(text: str) -> str:
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "..."


def _at(obj: Any, path: tuple[str, ...]) -> Any:
    """The value at *path* in *obj*; ``None`` when a step is missing or not a map."""
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _maps(value: Any) -> list[dict[str, Any]]:
    """The maps of the list *value*; an empty list for anything else."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


_Reader = Callable[[Any], list[str]]


def _uses_secret(name: Any) -> str:
    return f"uses Secret {_shown_name(name)}"


def _named_secrets(value: Any) -> list[str]:
    """A list of ``{name}`` references to Secrets."""
    return [_uses_secret(item.get("name")) for item in _maps(value)]


def _pull_secrets(value: Any) -> list[str]:
    """``imagePullSecrets``: a list of ``{name}`` references."""
    return [f"uses image pull Secret {_shown_name(item.get('name'))}" for item in _maps(value)]


def _tls_secrets(value: Any) -> list[str]:
    """``spec.tls`` of an Ingress: each entry names its Secret in ``secretName``."""
    return [_uses_secret(item["secretName"]) for item in _maps(value) if "secretName" in item]


def _volume_source(volume: Any) -> list[str]:
    """The Secrets and host paths a volume source names (a pod volume or a PersistentVolume)."""
    if not isinstance(volume, dict):
        return []
    found: list[str] = []
    secret = volume.get("secret")
    if isinstance(secret, dict) and "secretName" in secret:
        found.append(_uses_secret(secret["secretName"]))
    host_path = volume.get("hostPath")
    if isinstance(host_path, dict):
        path = host_path.get("path")
        found.append(
            f"hostPath volume {_short(path) if isinstance(path, str) else '(path not shown)'}"
        )
    csi = volume.get("csi")
    found.extend(
        _uses_secret(ref.get("name"))
        for ref in (_at(csi, (key,)) for key in _CSI_SECRET_REFS)
        if isinstance(ref, dict)
    )
    for source in _maps(_at(volume, ("projected", "sources"))):
        if isinstance(source.get("secret"), dict):
            found.append(_uses_secret(source["secret"].get("name")))
    azure_file = volume.get("azureFile")
    if isinstance(azure_file, dict) and "secretName" in azure_file:
        found.append(_uses_secret(azure_file["secretName"]))
    found.extend(
        _uses_secret(ref.get("name"))
        for ref in (_at(volume, (source, "secretRef")) for source in _SECRET_REF_SOURCES)
        if isinstance(ref, dict)
    )
    return found


def _container(container: dict[str, Any]) -> list[str]:
    """The Secrets, the host access and the host ports of one container."""
    found: list[str] = []
    for env in _maps(container.get("env")):
        ref = _at(env, ("valueFrom", "secretKeyRef"))
        if isinstance(ref, dict):
            found.append(_uses_secret(ref.get("name")))
    for env_from in _maps(container.get("envFrom")):
        ref = env_from.get("secretRef")
        if isinstance(ref, dict):
            found.append(_uses_secret(ref.get("name")))
    if _at(container, ("securityContext", "privileged")) is True:
        found.append("a container asks for full access to its host")
    for port in _maps(container.get("ports")):
        host_port = port.get("hostPort")
        if isinstance(host_port, int) and not isinstance(host_port, bool) and host_port:
            found.append(f"host port {host_port}")
    return found


def _pod_spec(spec: Any) -> list[str]:
    """The risky fields of a pod spec, from the fields Kubernetes defines in it."""
    if not isinstance(spec, dict):
        return []
    found: list[str] = []
    for key in ("serviceAccountName", "serviceAccount"):
        if isinstance(spec.get(key), str):
            found.append(f"runs as service account {_shown_name(spec[key])}")
    found.extend(f"{flag} is on" for flag in _HOST_FLAGS if spec.get(flag) is True)
    found.extend(_pull_secrets(spec.get("imagePullSecrets")))
    for volume in _maps(spec.get("volumes")):
        found.extend(_volume_source(volume))
    for key in _CONTAINER_LISTS:
        for container in _maps(spec.get(key)):
            found.extend(_container(container))
    return found


def _role_ref(kind: str) -> _Reader:
    """The reader of the ``roleRef`` of a role binding of *kind*: what it grants."""

    def _read(role_ref: Any) -> list[str]:
        if not isinstance(role_ref, dict):
            return [f"a {kind} (rights)"]
        ref_kind = role_ref.get("kind")
        shown_kind = ref_kind if ref_kind in _RBAC_ROLES else _NAME_NOT_SHOWN
        return [f"a {kind} that grants {shown_kind} {_shown_role_name(role_ref.get('name'))}"]

    return _read


def _service_exposure(spec: Any) -> list[str]:
    """The ``spec`` of a Service: reachable from outside by its type or external IPs."""
    if not isinstance(spec, dict):
        return []
    risks = []
    if spec.get("type") in _OUTSIDE_SERVICE_TYPES:
        risks.append(f"a Service of type {spec['type']} (reachable from outside)")
    if spec.get("externalIPs"):
        risks.append("a Service with external IPs (reachable from outside)")
    return risks


_TEMPLATE_POD = ("spec", "template", "spec")

#: Every field the summary reads, as ``(group, kind, path, reader)``. The
#: reader gets the value at *path* of the object. No other field of any
#: object is read: not the ``data`` or ``binaryData`` of a ConfigMap, not the
#: ``spec`` of a kind outside the Kubernetes groups, not labels or annotations.
_PLACES: tuple[tuple[str, str, tuple[str, ...], _Reader], ...] = (
    ("", "Pod", ("spec",), _pod_spec),
    ("", "PodTemplate", ("template", "spec"), _pod_spec),
    ("", "ReplicationController", _TEMPLATE_POD, _pod_spec),
    ("apps", "Deployment", _TEMPLATE_POD, _pod_spec),
    ("apps", "ReplicaSet", _TEMPLATE_POD, _pod_spec),
    ("apps", "StatefulSet", _TEMPLATE_POD, _pod_spec),
    ("apps", "DaemonSet", _TEMPLATE_POD, _pod_spec),
    ("batch", "Job", _TEMPLATE_POD, _pod_spec),
    ("batch", "CronJob", ("spec", "jobTemplate", *_TEMPLATE_POD), _pod_spec),
    ("", "ServiceAccount", ("secrets",), _named_secrets),
    ("", "ServiceAccount", ("imagePullSecrets",), _pull_secrets),
    ("", "PersistentVolume", ("spec",), _volume_source),
    ("", "Service", ("spec",), _service_exposure),
    ("networking.k8s.io", "Ingress", ("spec", "tls"), _tls_secrets),
    (_RBAC_GROUP, "RoleBinding", ("roleRef",), _role_ref("RoleBinding")),
    (_RBAC_GROUP, "ClusterRoleBinding", ("roleRef",), _role_ref("ClusterRoleBinding")),
)

#: The line a kind gives by itself, without reading any field.
_KIND_LINES: dict[tuple[str, str], str] = {
    ("", "ServiceAccount"): "a service account",
    (_RBAC_GROUP, "Role"): "a Role (rights)",
    (_RBAC_GROUP, "ClusterRole"): "a ClusterRole (rights)",
}


def risky_fields(group: str, kind: str, obj: dict[str, Any]) -> list[str]:
    """The risky fields of *obj*, the object as it would be after the apply.

    A Secret object is named but never read: its values are not looked at.
    Every other object is read only at its places of :data:`_PLACES`.
    """
    if group == "" and kind == "Secret":
        return ["a Secret object (its values are not shown)"]
    line = _KIND_LINES.get((group, kind))
    found = [line] if line else []
    for place_group, place_kind, path, reader in _PLACES:
        if (place_group, place_kind) == (group, kind):
            found.extend(reader(_at(obj, path)))
    found = list(dict.fromkeys(found))
    if len(found) > _MAX_ITEMS:
        return [*found[:_MAX_ITEMS], f"and {len(found) - _MAX_ITEMS} more"]
    return found


def other_writers(live: dict[str, Any], field_manager: str) -> list[dict[str, str]]:
    """Who else owns fields of *live*, from its ``managedFields``; not *field_manager*.

    A manager name shows only after its check, and an ``operation`` only as
    ``Apply`` or ``Update``.
    """
    metadata = live.get("metadata")
    entries = metadata.get("managedFields") if isinstance(metadata, dict) else None
    writers: dict[tuple[str, str], None] = {}
    for entry in _maps(entries):
        manager = entry.get("manager")
        if not isinstance(manager, str) or manager == field_manager:
            continue
        operation = entry.get("operation")
        shown_op = operation if operation in _WRITER_OPERATIONS else _NOT_SHOWN
        writers[(_shown_manager(manager), shown_op)] = None
    shown = [{"manager": manager, "operation": op} for manager, op in writers]
    if len(shown) > _MAX_ITEMS:
        return [*shown[:_MAX_ITEMS], {"manager": f"and {len(shown) - _MAX_ITEMS} more"}]
    return shown


async def _read_live(dyn: Any, resource: Any, name: Any, namespace: Any) -> dict[str, Any] | None:
    """Read the live object once. ``None`` when it does not exist (HTTP 404).

    Raises for any other answer: a failed read is never "does not exist".
    """
    response = await dyn.get(resource, name=name, namespace=namespace, serialize=False)
    if response.status == 404:
        return None
    if not 200 <= response.status <= 299:
        raise RuntimeError(f"the live object could not be read (HTTP {response.status})")
    parsed = json.loads(await response.text())
    if not isinstance(parsed, dict):
        raise RuntimeError("the live object could not be read (not an object)")
    return parsed


async def _live_state(
    dyn: Any, resource: Any, doc: dict[str, Any], field_manager: str, *, read_live: bool
) -> dict[str, Any]:
    """``state``, ``other_writers`` and ``state_note`` from one read of the live object."""
    if not read_live:
        return {"state": "not_read", "state_note": NOT_READ_TEXT, "other_writers": None}
    metadata = doc.get("metadata") or {}
    try:
        live = await _read_live(dyn, resource, metadata.get("name"), metadata.get("namespace"))
    except Exception as exc:
        _log.warning("k8s_apply_summary_live_read_failed", error=type(exc).__name__)
        return {
            "state": "unknown",
            "state_note": f"the live object could not be read ({type(exc).__name__})",
            "other_writers": None,
        }
    if live is None:
        return {"state": "new", "other_writers": []}
    return {"state": "exists", "other_writers": other_writers(live, field_manager)}


async def summarize_applied_object(
    dyn: Any,
    resource: Any,
    doc: dict[str, Any],
    applied: Any,
    *,
    field_manager: str,
    read_live: bool,
) -> dict[str, Any]:
    """The summary of one manifest document, after its dry run.

    *applied* is the object the dry run returned. Never raises: a part that
    cannot be made is marked in the entry. Reads the live object once when
    *read_live* is set, and sends no write.
    """
    api_version = str(doc.get("apiVersion", ""))
    kind = str(doc.get("kind", ""))
    metadata = doc.get("metadata") or {}
    group = _group_of(api_version)
    namespaced = getattr(resource, "namespaced", None)
    entry: dict[str, Any] = {
        "api_group": group or "core",
        "kind": kind,
        "namespace": metadata.get("namespace"),
        "name": metadata.get("name"),
        "built_in": kind in _BUILT_IN_KINDS.get(group, frozenset()),
        "cluster_wide": (not namespaced) if isinstance(namespaced, bool) else None,
    }
    entry.update(await _live_state(dyn, resource, doc, field_manager, read_live=read_live))
    if isinstance(applied, dict) and applied:
        try:
            entry["risky_fields"] = risky_fields(group, kind, applied)
        except Exception as exc:
            _log.warning("k8s_apply_summary_risks_failed", error=type(exc).__name__)
            entry["risky_fields"] = None
            entry["summary_unavailable"] = "the risky fields could not be read"
    else:
        entry["risky_fields"] = None
        entry["summary_unavailable"] = "the dry run returned no object"
    return entry
