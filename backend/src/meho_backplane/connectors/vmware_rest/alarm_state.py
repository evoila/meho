# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared reads for the vCenter alarm operations (#4383, part 1).

Three operations work with triggered alarms:

* ``vmware.alarm.triggered.list`` (a typed read) lists them;
* ``vmware.composite.alarm.acknowledge`` acknowledges one;
* ``vmware.composite.alarm.reset`` resets them to green.

All three need the same facts, so the reads live here, once:

* ``ServiceInstance.RetrieveServiceContent`` gives the ``rootFolder`` and the
  ``alarmManager`` of the target. Their ids are read, never guessed.
* ``PropertyCollector.RetrievePropertiesEx`` reads
  ``ManagedEntity.triggeredAlarmState`` of one object. vCenter copies every
  triggered alarm up the inventory tree, so the root folder holds the
  triggered alarms of the whole vCenter. Each entry names the object the
  alarm is triggered on, not the folder.
* A second and third ``RetrievePropertiesEx`` read the alarm names and rules
  (``Alarm.info.name``, ``Alarm.info.expression``) and the object names
  (``ManagedEntity.name``). These name reads are best effort: when one fails,
  the rows keep their ids and ``read_note`` says what is missing.

A ``RetrievePropertiesEx`` call can succeed while vCenter still could not
read a property: the property is then in the object's ``missingSet``, usually
with a fault such as ``NoPermission``. For ``triggeredAlarmState`` that is
never read as "no triggered alarms": :func:`read_triggered_alarms` raises
:class:`AlarmStateUnreadableError`, and it does the same when the answer has
no entry for the object. An unset property has no ``missingSet`` entry and
means no triggered alarms.

vCenter may also split a large answer into pages: a ``RetrieveResult`` with
some objects and a ``token``. Every read here follows each token with
``ContinueRetrievePropertiesEx``, on the same session, until a page comes back
without one. A page that fails makes the whole read fail, so a partial answer
is never taken for a whole one.

Every call goes through
:meth:`~meho_backplane.connectors.vmware_rest.connector.VmwareRestConnector._post_vmomi_json`
(VI-JSON on vCenter 8.0 U1 and newer). This module imports nothing from the
typed-op registry, so the typed read and the composites can both import it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest.session import SessionTokenUsed
from meho_backplane.connectors.vmware_rest.vim_body import (
    fault_type_name,
    retrieve_properties_body,
    unwrap_vim_value,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "ALARMS_GROUP_KEY",
    "ALARMS_WHEN_TO_USE",
    "ALARM_ENTITY_TYPES",
    "AlarmStateUnreadableError",
    "alarm_key",
    "read_service_content_refs",
    "read_triggered_alarms",
    "trigger_kind",
]

#: One operation group for the alarm read and the alarm changes. The typed
#: read and the two composites register under this key with this text.
ALARMS_GROUP_KEY: Final = "alarms"
ALARMS_WHEN_TO_USE: Final = (
    "Use for vCenter alarms. Read: list the alarms that are triggered now (red or "
    "yellow), for the whole vCenter or under one object, with the alarm name, the "
    "object, the time and who acknowledged it (vmware.alarm.triggered.list). "
    "Changes: acknowledge one triggered alarm on one object, so its actions stop "
    "(alarm.acknowledge), and reset triggered alarms to green after the cause is "
    "fixed (alarm.reset). vCenter resets alarms by status, object kind and trigger "
    "kind, not one by one, so one reset can clear more than one alarm."
)

#: The managed object types that carry ``triggeredAlarmState`` in the pinned
#: VI-JSON files of vCenter 8.0 U1 to U3 and 9.0 (the ``ManagedEntity``
#: subtypes).
ALARM_ENTITY_TYPES: Final[tuple[str, ...]] = (
    "ClusterComputeResource",
    "ComputeResource",
    "Datacenter",
    "Datastore",
    "DistributedVirtualPortgroup",
    "DistributedVirtualSwitch",
    "Folder",
    "HostSystem",
    "Network",
    "OpaqueNetwork",
    "ResourcePool",
    "StoragePod",
    "VirtualApp",
    "VirtualMachine",
    "VmwareDistributedVirtualSwitch",
)

# Spec-relative VI-JSON paths; ``_post_vmomi_json`` puts them on the
# ``/sdk/vim25/{release}`` base. Both singletons have a fixed moId.
_SERVICE_CONTENT_PATH: Final = "/ServiceInstance/ServiceInstance/RetrieveServiceContent"
_RETRIEVE_PROPERTIES_PATH: Final = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
_CONTINUE_RETRIEVE_PATH: Final = "/PropertyCollector/propertyCollector/ContinueRetrievePropertiesEx"
#: Most pages one property read follows: a guard against an answer that never ends.
_MAX_PAGES: Final = 100

_PROP_TRIGGERED: Final = "triggeredAlarmState"
_PROP_ALARM_NAME: Final = "info.name"
_PROP_ALARM_EXPRESSION: Final = "info.expression"
_PROP_NAME: Final = "name"
_ALARM_MO_TYPE: Final = "Alarm"

# Alarm rule (``AlarmExpression``) types of the pinned VI-JSON files.
# vCenter's reset filter calls metric and state rules "metric" together
# (``triggerTypeMetric``: "Metric or state alarms").
_EVENT_EXPRESSION: Final = "EventAlarmExpression"
_METRIC_EXPRESSIONS: Final = frozenset({"MetricAlarmExpression", "StateAlarmExpression"})
_GROUP_EXPRESSIONS: Final = frozenset({"AndAlarmExpression", "OrAlarmExpression"})
# A guard against a broken or looping rule tree; real rules are 1-3 deep.
_MAX_EXPRESSION_DEPTH: Final = 16

# Red rows first, then yellow, then anything else.
_STATUS_ORDER: Final[dict[str, int]] = {"red": 0, "yellow": 1}


def _moref_value(ref: Any) -> str | None:
    """Return the moid of a VI-JSON MoRef, else ``None``."""
    if isinstance(ref, dict):
        value = ref.get("value")
        return value if isinstance(value, str) and value else None
    return None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


class AlarmStateUnreadableError(RuntimeError):
    """vCenter answered, but did not give the list of triggered alarms.

    The ``triggeredAlarmState`` property is in the answer's ``missingSet`` (for
    example ``NoPermission``), or the answer has no entry for the object. The
    message names the property and the fault type, never the fault text.
    """


def alarm_key(row: dict[str, Any]) -> tuple[str, str]:
    """The ``(alarm_id, entity_id)`` pair that names one triggered alarm."""
    return row["alarm_id"], row["entity_id"]


async def read_service_content_refs(
    connector: VmwareRestConnector, target: VsphereTargetLike, operator: Operator
) -> tuple[str, str | None]:
    """Return ``(root_folder_moid, alarm_manager_moid)`` from the ServiceContent.

    The alarm manager is ``None`` when the target has none. A ServiceContent
    without a root folder is an error: nothing can be read then.
    """
    raw = await connector._post_vmomi_json(
        target, _SERVICE_CONTENT_PATH, operator=operator, json={}
    )
    content = unwrap_vim_value(raw)
    if not isinstance(content, dict):
        raise RuntimeError(f"RetrieveServiceContent returned no ServiceContent ({type(raw)})")
    root_folder = _moref_value(content.get("rootFolder"))
    if root_folder is None:
        raise RuntimeError("ServiceContent has no rootFolder")
    return root_folder, _moref_value(content.get("alarmManager"))


def trigger_kind(expression: Any) -> str | None:
    """Return the trigger kind of an alarm rule: ``event``, ``metric`` or ``mixed``.

    ``event`` when every condition of the rule is an event condition,
    ``metric`` when every condition is a metric or state condition, and
    ``mixed`` when the rule has both. ``None`` when the rule is missing or has
    a condition type this module does not know.
    """
    kinds: set[str] = set()
    _collect_kinds(expression, kinds, 0)
    if not kinds or "unknown" in kinds:
        return None
    if kinds == {"event"}:
        return "event"
    if kinds == {"metric"}:
        return "metric"
    return "mixed"


def _collect_kinds(node: Any, kinds: set[str], depth: int) -> None:
    if depth > _MAX_EXPRESSION_DEPTH or not isinstance(node, dict):
        kinds.add("unknown")
        return
    type_name = node.get("_typeName")
    if type_name == _EVENT_EXPRESSION:
        kinds.add("event")
    elif type_name in _METRIC_EXPRESSIONS:
        kinds.add("metric")
    elif type_name in _GROUP_EXPRESSIONS:
        children = node.get("expression")
        if not isinstance(children, list) or not children:
            kinds.add("unknown")
            return
        for child in children:
            _collect_kinds(child, kinds, depth + 1)
    else:
        kinds.add("unknown")


def _objects(retrieve_result: Any) -> list[dict[str, Any]]:
    payload = unwrap_vim_value(retrieve_result)
    if isinstance(payload, dict):
        payload = payload.get("objects", [])
    if not isinstance(payload, list):
        return []
    return [obj for obj in payload if isinstance(obj, dict)]


def _props_by_moid(retrieve_result: Any) -> dict[str, dict[str, Any]]:
    """Map each object's moid to its ``{property path: value}`` pairs."""
    found: dict[str, dict[str, Any]] = {}
    for obj in _objects(retrieve_result):
        moid = _moref_value(obj.get("obj"))
        if moid is None:
            continue
        props = found.setdefault(moid, {})
        for prop in obj.get("propSet", []) or []:
            if isinstance(prop, dict) and isinstance(prop.get("name"), str):
                props[prop["name"]] = unwrap_vim_value(prop.get("val"))
    return found


def _token(retrieve_result: Any) -> str | None:
    """The continuation ``token`` of a ``RetrieveResult``, or ``None`` on the last page."""
    payload = unwrap_vim_value(retrieve_result)
    token = payload.get("token") if isinstance(payload, dict) else None
    return token if isinstance(token, str) and token else None


async def _retrieve_all_pages(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    operator: Operator,
    body: dict[str, Any],
    *,
    promote_missing: bool = False,
) -> dict[str, Any]:
    """Run one ``RetrievePropertiesEx`` and follow every continuation page.

    Each ``token`` goes once to ``ContinueRetrievePropertiesEx``, on the session
    the first call used (vCenter accepts a token only there), until a page
    has no token. Returns ``{"objects": [...]}`` with the objects of all pages.
    A failed page raises; so does an answer that has not ended after
    :data:`_MAX_PAGES` pages (:class:`AlarmStateUnreadableError`).
    """
    session = SessionTokenUsed()
    result = await connector._post_vmomi_json(
        target,
        _RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=body,
        promote_managed_object_not_found=promote_missing,
        session_used=session,
    )
    objects = _objects(result)
    token = _token(result)
    pages = 1
    while token is not None:
        if pages >= _MAX_PAGES:
            raise AlarmStateUnreadableError(
                f"vCenter's answer had not ended after {_MAX_PAGES} pages; the read is incomplete"
            )
        result = await connector._post_vmomi_json(
            target,
            _CONTINUE_RETRIEVE_PATH,
            operator=operator,
            json={"token": token},
            session_token=session.token,
        )
        objects.extend(_objects(result))
        token = _token(result)
        pages += 1
    return {"objects": objects}


def _unreadable_by_moid(retrieve_result: Any) -> dict[str, dict[str, str]]:
    """Map each object's moid to ``{property path: fault type}`` from its ``missingSet``.

    A property vCenter could not read is not in ``propSet``; it is in the
    object's ``missingSet``, usually with a fault. An unset property has no
    entry. Only the fault type name is kept (``unknown fault`` when there is
    none), never the fault text, which can echo a private value.
    """
    found: dict[str, dict[str, str]] = {}
    for obj in _objects(retrieve_result):
        moid = _moref_value(obj.get("obj"))
        if moid is None:
            continue
        for entry in obj.get("missingSet", []) or []:
            path = entry.get("path") if isinstance(entry, dict) else None
            if isinstance(path, str):
                fault_type = fault_type_name(entry.get("fault")) or "unknown fault"
                found.setdefault(moid, {})[path] = fault_type
    return found


def _state_row(state: Any) -> dict[str, Any] | None:
    """Turn one ``AlarmState`` into a flat row.

    ``None`` when the entry names no alarm, or no object with its type: the
    changes need the full object reference.
    """
    if not isinstance(state, dict):
        return None
    alarm_id = _moref_value(state.get("alarm"))
    entity = state.get("entity")
    entity_id = _moref_value(entity)
    entity_type = _str_or_none(entity.get("type")) if isinstance(entity, dict) else None
    if alarm_id is None or entity_id is None or entity_type is None:
        return None
    acknowledged = state.get("acknowledged") is True
    return {
        "alarm_id": alarm_id,
        "alarm_name": None,
        "trigger_kind": None,
        "entity_id": entity_id,
        "entity_type": entity_type,
        "entity_name": None,
        "status": _str_or_none(state.get("overallStatus")),
        "time": _str_or_none(state.get("time")),
        "acknowledged": acknowledged,
        # vCenter says these two are not valid until the alarm is acknowledged.
        "acknowledged_by": _str_or_none(state.get("acknowledgedByUser")) if acknowledged else None,
        "acknowledged_time": _str_or_none(state.get("acknowledgedTime")) if acknowledged else None,
    }


async def read_triggered_alarms(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    operator: Operator,
    *,
    mo_type: str,
    moid: str,
    with_names: bool = True,
    promote_missing: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    """Read the triggered alarms of one object and the objects below it.

    Returns ``(rows, read_note)``: one flat row per triggered alarm, red rows
    first and the newest first within a status, and a note that names the
    name reads that failed (``None`` when all worked). ``with_names=False``
    skips the name reads (a read-back after a change only needs the ids).
    ``promote_missing=True`` turns a missing object into MEHO's normal
    ``not_found`` answer.

    Raises :class:`AlarmStateUnreadableError` when vCenter could not read the
    list (a ``missingSet`` entry) or sent no entry for the object, so a
    caller never takes an unreadable list for an empty one. A failed
    continuation page raises too.
    """
    result = await _retrieve_all_pages(
        connector,
        target,
        operator,
        retrieve_properties_body(mo_type, [moid], [_PROP_TRIGGERED]),
        promote_missing=promote_missing,
    )
    unreadable = _unreadable_by_moid(result).get(moid)
    if unreadable:
        detail = ", ".join(f"{path} ({fault})" for path, fault in sorted(unreadable.items()))
        raise AlarmStateUnreadableError(
            f"vCenter could not read {detail} of {mo_type} {moid}; "
            "the triggered alarms are not known"
        )
    props = _props_by_moid(result).get(moid)
    if props is None:
        raise AlarmStateUnreadableError(
            f"vCenter's answer has no entry for {mo_type} {moid}; "
            "the triggered alarms are not known"
        )
    states = props.get(_PROP_TRIGGERED)
    rows = [row for row in (_state_row(s) for s in states or []) if row is not None]
    note = await _add_names(connector, target, operator, rows) if with_names and rows else None
    rows.sort(key=lambda row: row["time"] or "", reverse=True)
    rows.sort(key=lambda row: _STATUS_ORDER.get(row["status"] or "", 2))
    return rows, note


async def _add_names(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    operator: Operator,
    rows: list[dict[str, Any]],
) -> str | None:
    """Fill ``alarm_name``, ``trigger_kind`` and ``entity_name``; return a note on failure."""
    notes: list[str] = []
    alarm_ids = sorted({row["alarm_id"] for row in rows})
    alarm_paths = [_PROP_ALARM_NAME, _PROP_ALARM_EXPRESSION]
    alarm_props = await _read_props(
        connector, target, operator, _ALARM_MO_TYPE, alarm_ids, alarm_paths, notes
    )
    for row in rows:
        props = alarm_props.get(row["alarm_id"], {})
        row["alarm_name"] = _str_or_none(props.get(_PROP_ALARM_NAME))
        row["trigger_kind"] = trigger_kind(props.get(_PROP_ALARM_EXPRESSION))

    ids_by_type: dict[str, set[str]] = {}
    for row in rows:
        ids_by_type.setdefault(row["entity_type"], set()).add(row["entity_id"])
    names: dict[tuple[str, str], str] = {}
    for mo_type, ids in sorted(ids_by_type.items()):
        props_by_moid = await _read_props(
            connector, target, operator, mo_type, sorted(ids), [_PROP_NAME], notes
        )
        for entity_id, props in props_by_moid.items():
            name = _str_or_none(props.get(_PROP_NAME))
            if name is not None:
                names[(mo_type, entity_id)] = name
    for row in rows:
        row["entity_name"] = names.get((row["entity_type"], row["entity_id"]))
    return "; ".join(notes) or None


async def _read_props(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    operator: Operator,
    mo_type: str,
    moids: list[str],
    paths: list[str],
    notes: list[str],
) -> dict[str, dict[str, Any]]:
    """One best-effort property read of all pages; a failure adds a note and returns ``{}``.

    A failed page counts as a failed read: the note names it, and no names of
    that type are used.
    """
    try:
        result = await _retrieve_all_pages(
            connector, target, operator, retrieve_properties_body(mo_type, moids, paths)
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        notes.append(f"{mo_type} names not read ({type(exc).__name__})")
        return {}
    unreadable = _unreadable_by_moid(result)
    if unreadable:
        faults = sorted({fault for paths in unreadable.values() for fault in paths.values()})
        notes.append(
            f"{mo_type} names not read for {len(unreadable)} of {len(moids)} objects "
            f"({', '.join(faults)})"
        )
    return _props_by_moid(result)
