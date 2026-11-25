# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The per-object summary on the approval card of ``k8s.apply``.

A person who approves an apply reads the manifest and the dry run. For a
manifest with many objects that is a lot, and the result depends on what is
already on the cluster. This module builds a short summary for each object,
so the person can decide faster. It is information only: no rule changes
and nothing runs without approval.

For each object the summary holds:

* ``api_group``, ``kind``, ``namespace`` and ``name``. A group of ``core``
  is the group of the old ``v1`` kinds.
* ``state``: ``new`` or ``exists``, from one read of the live object. A
  failed read gives ``unknown`` and a ``state_note`` that says why. It is
  never shown as ``new``.
* ``other_writers``: who else owns fields of the live object (its
  ``managedFields``), without the ``meho`` manager. ``None`` when the live
  object could not be read.
* ``risky_fields``: what is risky in the object as it would be after the
  apply (the object that the dry run returns): a Secret or a reference to
  one, a service account, a role or role binding, host access, a Service
  that is reachable from outside. ``None`` when the dry run returned no
  object.
* ``built_in``: ``False`` for a kind of an API group that is not a Kubernetes
  group. The group is always read together with the kind.
* ``cluster_wide``: ``True`` for a kind that is not in a namespace.

The summary holds names only. It never holds a Secret value, an ``env``
value, a command line or any other value of the object, and the live
object is read only for its ``managedFields``.

The builder of the approval card turns the summary on by setting
:data:`apply_summaries_var`. Only a person's card does this: the preview an
agent reads has no summary, and it makes no extra read.
"""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from typing import Any

import structlog

__all__ = [
    "OBJECT_SUMMARY_KEY",
    "SUMMARY_FAILED_TEXT",
    "apply_summaries_var",
    "summarize_applied_object",
]

_log = structlog.get_logger(__name__)

#: The key of the ``k8s.apply`` preview that holds the summary list. Only a
#: person reads it.
OBJECT_SUMMARY_KEY = "object_summary"

#: What the card says when the dry run failed and no summary could be made.
SUMMARY_FAILED_TEXT = "The summary of the objects could not be made, because the dry run failed."

#: Set to a list by the builder of the approval card. ``_apply_one`` adds the
#: summary of each object of a dry run to it. ``None`` (the default): no
#: summary and no extra read.
apply_summaries_var: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "k8s_apply_summaries", default=None
)

#: The API groups of Kubernetes itself. ``""`` is the core group. A kind of
#: any other group is not a built-in kind. No kind and no vendor is named.
_BUILT_IN_GROUPS = frozenset(
    {
        "",
        "admissionregistration.k8s.io",
        "apiextensions.k8s.io",
        "apiregistration.k8s.io",
        "apps",
        "authentication.k8s.io",
        "authorization.k8s.io",
        "autoscaling",
        "batch",
        "certificates.k8s.io",
        "coordination.k8s.io",
        "discovery.k8s.io",
        "events.k8s.io",
        "flowcontrol.apiserver.k8s.io",
        "internal.apiserver.k8s.io",
        "networking.k8s.io",
        "node.k8s.io",
        "policy",
        "rbac.authorization.k8s.io",
        "resource.k8s.io",
        "scheduling.k8s.io",
        "storage.k8s.io",
        "storagemigration.k8s.io",
    }
)

_RBAC_GROUP = "rbac.authorization.k8s.io"
_RBAC_KINDS = frozenset({"Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"})
_HOST_FLAGS = ("hostNetwork", "hostPID", "hostIPC")
_OUTSIDE_SERVICE_TYPES = frozenset({"NodePort", "LoadBalancer"})

#: Most risky fields and other writers named for one object, and the longest
#: text of one entry.
_MAX_ITEMS = 20
_MAX_TEXT = 200
_MAX_DEPTH = 40

#: A Kubernetes object name. Anything else is not shown as a name.
_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")


def _group_of(api_version: str) -> str:
    """The API group of an ``apiVersion``: ``apps`` of ``apps/v1``, ``""`` of ``v1``."""
    return api_version.rpartition("/")[0]


def _shown_name(value: Any) -> str:
    """*value* when it is an object name, else a fixed text. Never an arbitrary string."""
    if isinstance(value, str) and _NAME_RE.match(value):
        return value
    return "(name not shown)"


def _short(text: str) -> str:
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "..."


def _secret_refs(key: str, value: Any) -> list[str]:
    """What a Secret reference field names, for the field *key* with *value*."""
    if key in ("secretKeyRef", "secretRef") and isinstance(value, dict):
        return [f"uses Secret {_shown_name(value.get('name'))}"]
    if key == "secretName" and isinstance(value, str):
        return [f"uses Secret {_shown_name(value)}"]
    if key == "secret" and isinstance(value, dict) and "name" in value:
        return [f"uses Secret {_shown_name(value.get('name'))}"]
    if key == "imagePullSecrets" and isinstance(value, list):
        return [
            f"uses image pull Secret {_shown_name(item.get('name'))}"
            for item in value
            if isinstance(item, dict)
        ]
    return []


def _host_access(key: str, value: Any) -> list[str]:
    """Host access named by the field *key* with *value*."""
    if key in _HOST_FLAGS and value is True:
        return [f"{key} is on"]
    if key == "privileged" and value is True:
        return ["a container is privileged"]
    if key == "hostPath" and isinstance(value, dict):
        path = value.get("path")
        return [f"hostPath volume {_short(path) if isinstance(path, str) else '(path not shown)'}"]
    if key == "hostPort" and isinstance(value, int) and not isinstance(value, bool) and value:
        return [f"host port {value}"]
    return []


def _service_account(key: str, value: Any) -> list[str]:
    if key in ("serviceAccountName", "serviceAccount") and isinstance(value, str):
        return [f"runs as service account {_shown_name(value)}"]
    return []


def _field_risks(key: str, value: Any) -> list[str]:
    return _secret_refs(key, value) + _host_access(key, value) + _service_account(key, value)


def _walk_risks(node: Any) -> list[str]:
    """Risky fields at any depth of *node*, in document order, without repeats."""
    found: list[str] = []
    stack: list[tuple[Any, int]] = [(node, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > _MAX_DEPTH:
            continue
        if isinstance(current, dict):
            children = []
            for key, value in current.items():
                found.extend(_field_risks(str(key), value))
                children.append((value, depth + 1))
            stack.extend(reversed(children))
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in reversed(current))
    return list(dict.fromkeys(found))


def _kind_risks(group: str, kind: str, obj: dict[str, Any]) -> list[str]:
    """Risks that come from the kind itself: Secret, service account, rights, a Service."""
    raw_spec = obj.get("spec")
    spec: dict[str, Any] = raw_spec if isinstance(raw_spec, dict) else {}
    if group == "" and kind == "Secret":
        return ["a Secret object (its values are not shown)"]
    if group == "" and kind == "ServiceAccount":
        return ["a service account"]
    if group == _RBAC_GROUP and kind in _RBAC_KINDS:
        role_ref = obj.get("roleRef")
        if isinstance(role_ref, dict):
            ref_kind = _short(str(role_ref.get("kind", "role")))
            return [f"a {kind} that grants {ref_kind} {_shown_name(role_ref.get('name'))}"]
        return [f"a {kind} (rights)"]
    if group == "" and kind == "Service":
        risks = []
        if spec.get("type") in _OUTSIDE_SERVICE_TYPES:
            risks.append(f"a Service of type {spec['type']} (reachable from outside)")
        if spec.get("externalIPs"):
            risks.append("a Service with external IPs (reachable from outside)")
        return risks
    return []


def risky_fields(group: str, kind: str, obj: dict[str, Any]) -> list[str]:
    """The risky fields of *obj*, the object as it would be after the apply.

    A Secret object is named but never read: its values are not looked at.
    """
    found = _kind_risks(group, kind, obj)
    if not (group == "" and kind == "Secret"):
        body = {key: value for key, value in obj.items() if key != "metadata"}
        found.extend(_walk_risks(body))
    found = list(dict.fromkeys(found))
    if len(found) > _MAX_ITEMS:
        return [*found[:_MAX_ITEMS], f"and {len(found) - _MAX_ITEMS} more"]
    return found


def other_writers(live: dict[str, Any], field_manager: str) -> list[dict[str, str]]:
    """Who else owns fields of *live*, from its ``managedFields``; not *field_manager*."""
    metadata = live.get("metadata")
    entries = metadata.get("managedFields") if isinstance(metadata, dict) else None
    writers: dict[tuple[str, str], None] = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        manager = entry.get("manager")
        if not isinstance(manager, str) or manager == field_manager:
            continue
        operation = entry.get("operation")
        writers[(_short(manager), operation if isinstance(operation, str) else "")] = None
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
    dyn: Any, resource: Any, doc: dict[str, Any], field_manager: str
) -> dict[str, Any]:
    """``state``, ``other_writers`` and ``state_note`` from one read of the live object."""
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
) -> dict[str, Any]:
    """The summary of one manifest document, after its dry run.

    *applied* is the object the dry run returned. Never raises: a part that
    cannot be made is marked in the entry. Reads the live object once and
    sends no write.
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
        "built_in": group in _BUILT_IN_GROUPS,
        "cluster_wide": (not namespaced) if isinstance(namespaced, bool) else None,
    }
    entry.update(await _live_state(dyn, resource, doc, field_manager))
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
