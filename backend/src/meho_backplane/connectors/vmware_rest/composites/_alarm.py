# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Alarm changes ``vmware.composite.alarm.acknowledge`` and ``.reset`` (#4383, part 1).

* ``alarm.acknowledge`` acknowledges one triggered alarm on one object
  (``AlarmManager.AcknowledgeAlarm``). The alarm stays red or yellow, but its
  actions (emails, SNMP traps) do not fire again until it has been green or
  gray once.
* ``alarm.reset`` resets triggered alarms to green
  (``AlarmManager.ClearTriggeredAlarms``). The public vSphere API has no call
  that resets one alarm on one object. vCenter's own manual reset uses
  ``AlarmManager.setAlarmStatus`` (the pinned spec names it once, in the note
  on ``AlarmState.eventKey``), but that call is in neither pyVmomi 8.0.3 nor
  the pinned VI-JSON files of vCenter 8.0 U1 to U3 and 9.0, so MEHO cannot
  call or check it. The reset therefore takes the vendor filter
  (``AlarmFilterSpec``: status, object kind, trigger kind) and clears every
  triggered alarm in the vCenter that matches it. The answer lists the alarms
  it cleared (read before and after the call).

Both are ``caution`` with ``requires_approval=True``: every call waits for an
approval, like every other VMware change. They change only the state of
alarms, no configuration and no workload. Schemas and AI guidance are in
:mod:`._alarm_schemas`.

Flow of both handlers, on the connector session:

1. ``ServiceInstance.RetrieveServiceContent`` -> root folder + alarm manager.
2. ``RetrievePropertiesEx`` of the root folder's ``triggeredAlarmState``
   (plus the alarm and object names, following every continuation page with
   ``ContinueRetrievePropertiesEx``) -> what is triggered now.
3. The one change, through the governed VI-JSON write seam
   (:func:`._write._write_vmomi_sub_op`, the #2254 sub-op gate).
4. The same read again -> the answer says what changed.

Both are safe to run twice: an alarm that is already acknowledged, or no
matching triggered alarm, gives ``unchanged`` and sends no change.

When vCenter cannot give the list of triggered alarms
(:class:`~meho_backplane.connectors.vmware_rest.alarm_state.AlarmStateUnreadableError`,
for example a ``NoPermission`` fault on the property), the read before the
change fails the call and nothing is changed; the read after the change gives
``not_verified``, never "cleared".

The approval previews read vCenter, so they run only at park time, when the
dispatcher passes the connector. ``preview_operation`` passes none: the
builders then decline, and the preview says that the plan is shown on the
approval request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.alarm_state import (
    alarm_key,
    read_service_content_refs,
    read_triggered_alarms,
)
from meho_backplane.connectors.vmware_rest.composite_issues import issue
from meho_backplane.connectors.vmware_rest.composites._write import _write_vmomi_sub_op
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY, vim_moref

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "alarm_acknowledge_composite",
    "alarm_acknowledge_preview",
    "alarm_reset_composite",
    "alarm_reset_preview",
]

# VIM (VI-JSON) op ids, the ``METHOD:/path`` keys the ingest parser emits from
# ``vi-json.yaml``. The two changes go to ``enforce_subop_policy`` with these
# keys; the reads are listed so the reconcile lanes check every path.
_OP_RETRIEVE_SERVICE_CONTENT: Final = "POST:/ServiceInstance/{moId}/RetrieveServiceContent"
_OP_RETRIEVE_PROPERTIES: Final = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
_OP_CONTINUE_RETRIEVE_PROPERTIES: Final = (
    "POST:/PropertyCollector/{moId}/ContinueRetrievePropertiesEx"
)
_OP_ACKNOWLEDGE_ALARM: Final = "POST:/AlarmManager/{moId}/AcknowledgeAlarm"
_OP_CLEAR_TRIGGERED_ALARMS: Final = "POST:/AlarmManager/{moId}/ClearTriggeredAlarms"

_VIM_SUB_OPS_ALARM_ACKNOWLEDGE: tuple[str, ...] = (
    _OP_RETRIEVE_SERVICE_CONTENT,
    _OP_RETRIEVE_PROPERTIES,
    _OP_CONTINUE_RETRIEVE_PROPERTIES,
    _OP_ACKNOWLEDGE_ALARM,
)
_VIM_SUB_OPS_ALARM_RESET: tuple[str, ...] = (
    _OP_RETRIEVE_SERVICE_CONTENT,
    _OP_RETRIEVE_PROPERTIES,
    _OP_CONTINUE_RETRIEVE_PROPERTIES,
    _OP_CLEAR_TRIGGERED_ALARMS,
)

#: The ``_typeName`` of the reset filter (a DataObject in the request body).
_ALARM_FILTER_SPEC_TYPE: Final = "AlarmFilterSpec"
_ALARM_MO_TYPE: Final = "Alarm"
_ROOT_FOLDER_MO_TYPE: Final = "Folder"

# Operator-facing filter values -> the vendor enum values of ``AlarmFilterSpec``.
_OBJECT_KIND_VENDOR: Final[dict[str, str]] = {
    "all": "entityTypeAll",
    "host": "entityTypeHost",
    "vm": "entityTypeVm",
}
_TRIGGER_KIND_VENDOR: Final[dict[str, str]] = {
    "all": "triggerTypeAll",
    "event": "triggerTypeEvent",
    "metric": "triggerTypeMetric",
}
# The object type each narrow object kind matches.
_OBJECT_KIND_TYPES: Final[dict[str, str]] = {"host": "HostSystem", "vm": "VirtualMachine"}

#: Most matching alarms one approval preview lists.
_PREVIEW_CAP: Final = 25


async def _root_alarms(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    root_folder: str,
    *,
    with_names: bool = True,
) -> list[dict[str, Any]]:
    """The triggered alarms of the whole vCenter (the root folder's list)."""
    rows, _ = await read_triggered_alarms(
        connector,
        target,
        operator,
        mo_type=_ROOT_FOLDER_MO_TYPE,
        moid=root_folder,
        with_names=with_names,
    )
    return rows


def _find(rows: list[dict[str, Any]], alarm_id: str, entity_id: str) -> dict[str, Any] | None:
    for row in rows:
        if alarm_key(row) == (alarm_id, entity_id):
            return row
    return None


def _error(status: str, message: str, guidance: str, **fields: Any) -> dict[str, Any]:
    """An answer with an ``error`` issue: the dispatcher records a failed call."""
    return {
        "status": status,
        **fields,
        "issues": [issue("connector", "error", message)],
        "guidance": guidance,
    }


def _no_alarm_manager(**fields: Any) -> dict[str, Any]:
    return _error(
        "no_alarm_manager",
        "the target has no alarm manager; nothing was changed",
        "Alarms work on a vCenter target. Call this on the vCenter.",
        **fields,
    )


# ===========================================================================
# alarm.acknowledge
# ===========================================================================


async def alarm_acknowledge_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Acknowledge one triggered alarm on one object (``AcknowledgeAlarm``).

    Op-id: ``vmware.composite.alarm.acknowledge``. Reads the triggered alarms
    first. ``not_triggered`` (with an error issue) when no triggered alarm has
    this alarm id and object id; ``unchanged`` when it is already acknowledged.
    Otherwise sends ``AcknowledgeAlarm`` through the governed write seam (a
    parked or denied gate comes back as is), reads again and returns
    ``acknowledged`` with who and when, or ``not_verified`` (with an error
    issue) when the read fails or does not show it acknowledged. A vendor
    fault on the change, or a failed read before it, raises, and the
    dispatcher reports it as ``connector_error``.
    """
    alarm_id = str(params["alarm_id"])
    entity_id = str(params["entity_id"])
    root_folder, alarm_manager = await read_service_content_refs(connector, target, operator)
    if alarm_manager is None:
        return _no_alarm_manager(alarm=None)
    row = _find(await _root_alarms(connector, target, operator, root_folder), alarm_id, entity_id)
    if row is None:
        return _error(
            "not_triggered",
            f"no triggered alarm {alarm_id} on {entity_id}; nothing was changed",
            "Call vmware.alarm.triggered.list and take alarm_id and entity_id from one row.",
            alarm=None,
        )
    if row["acknowledged"]:
        return {"status": "unchanged", "alarm": row, "guidance": "It was already acknowledged."}

    gate, _ = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_ACKNOWLEDGE_ALARM,
        vmomi_path=f"/AlarmManager/{alarm_manager}/AcknowledgeAlarm",
        body={
            "alarm": vim_moref(_ALARM_MO_TYPE, alarm_id),
            "entity": vim_moref(row["entity_type"], entity_id),
        },
        params={"alarm": alarm_id, "entity": entity_id},
    )
    if gate is not None:
        return gate

    try:
        after = await _root_alarms(connector, target, operator, root_folder, with_names=False)
    except (httpx.HTTPError, RuntimeError) as exc:
        after, read_failure = [], f" ({type(exc).__name__})"
    else:
        read_failure = ""
    after_row = _find(after, alarm_id, entity_id)
    if after_row is None or not after_row["acknowledged"]:
        return _error(
            "not_verified",
            f"MEHO sent the acknowledge for alarm {alarm_id} on {entity_id}, but a new "
            f"read{read_failure} does not show it acknowledged; the change is not confirmed",
            "Call vmware.alarm.triggered.list to see the state now.",
            alarm=row,
        )
    acknowledged = {
        **row,
        "acknowledged": True,
        "acknowledged_by": after_row["acknowledged_by"],
        "acknowledged_time": after_row["acknowledged_time"],
    }
    return {"status": "acknowledged", "alarm": acknowledged, "guidance": None}


async def alarm_acknowledge_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview for the approver: the alarm as it is now (live read).

    Declines (``None``) without a connector, as in ``preview_operation``: the
    alarm can only be shown after a read of vCenter. ``alarm`` is ``None``
    when the alarm is not triggered now (the call will then change nothing).
    """
    alarm_id = ctx.params.get("alarm_id")
    entity_id = ctx.params.get("entity_id")
    if not isinstance(alarm_id, str) or not isinstance(entity_id, str):
        return None
    if ctx.connector_instance is None:
        return None
    preview: dict[str, Any] = {
        "action": "acknowledge_alarm",
        "alarm_id": alarm_id,
        "entity_id": entity_id,
    }
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    root_folder, _ = await read_service_content_refs(connector, ctx.target, ctx.operator)
    rows = await _root_alarms(connector, ctx.target, ctx.operator, root_folder)
    preview["alarm"] = _find(rows, alarm_id, entity_id)
    return preview


# ===========================================================================
# alarm.reset
# ===========================================================================


def _reset_filter(params: dict[str, Any]) -> dict[str, Any]:
    """The operator-facing filter, normalised (sorted unique statuses)."""
    return {
        "status": sorted({str(s) for s in params["status"]}),
        "object_kind": str(params["object_kind"]),
        "trigger_kind": str(params["trigger_kind"]),
    }


def _filter_spec(reset_filter: dict[str, Any]) -> dict[str, Any]:
    """The vendor ``AlarmFilterSpec`` for ``ClearTriggeredAlarms``."""
    return {
        VIM_TYPE_NAME_KEY: _ALARM_FILTER_SPEC_TYPE,
        "status": list(reset_filter["status"]),
        "typeEntity": _OBJECT_KIND_VENDOR[reset_filter["object_kind"]],
        "typeTrigger": _TRIGGER_KIND_VENDOR[reset_filter["trigger_kind"]],
    }


def _matches_status_and_object(row: dict[str, Any], reset_filter: dict[str, Any]) -> bool:
    if row["status"] not in reset_filter["status"]:
        return False
    object_type = _OBJECT_KIND_TYPES.get(reset_filter["object_kind"])
    return object_type is None or row["entity_type"] == object_type


def _matches(row: dict[str, Any], reset_filter: dict[str, Any]) -> bool:
    """True when the filter catches the row as far as MEHO can tell.

    An alarm whose trigger kind is not known, or is ``mixed``, counts as a
    match: vCenter may reset it.
    """
    if not _matches_status_and_object(row, reset_filter):
        return False
    wanted = reset_filter["trigger_kind"]
    kind = row["trigger_kind"]
    return wanted == "all" or kind not in ("event", "metric") or kind == wanted


def _reset_outcome(
    reset_filter: dict[str, Any],
    before: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> dict[str, Any]:
    """The answer after the reset, from the reads before and after it."""
    after_keys = {alarm_key(row) for row in after}
    cleared = [
        row
        for row in before
        if _matches_status_and_object(row, reset_filter) and alarm_key(row) not in after_keys
    ]
    still_triggered = [row for row in candidates if alarm_key(row) in after_keys]
    return {
        "status": "reset" if cleared else "unchanged",
        "filter": reset_filter,
        "cleared": cleared,
        "still_triggered": still_triggered,
        "guidance": (
            "An alarm in still_triggered fired again at once (its cause is still there) or "
            "vCenter did not count it under this trigger kind."
            if still_triggered
            else None
        ),
    }


async def alarm_reset_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Reset the triggered alarms that match a filter to green (``ClearTriggeredAlarms``).

    Op-id: ``vmware.composite.alarm.reset``. Reads the triggered alarms first;
    ``unchanged`` with no change sent when none matches the filter. Otherwise
    sends ``ClearTriggeredAlarms`` through the governed write seam (a parked or
    denied gate comes back as is) and reads again: ``cleared`` lists the alarms
    with the asked status and object kind that are no longer triggered, and
    ``still_triggered`` the matching alarms that are. ``reset`` when at least
    one alarm was cleared, else ``unchanged``; ``not_verified`` (with an error
    issue) when the read after the change fails.
    """
    reset_filter = _reset_filter(params)
    empty: dict[str, Any] = {"filter": reset_filter, "cleared": [], "still_triggered": []}
    root_folder, alarm_manager = await read_service_content_refs(connector, target, operator)
    if alarm_manager is None:
        return _no_alarm_manager(**empty)
    before = await _root_alarms(connector, target, operator, root_folder)
    candidates = [row for row in before if _matches(row, reset_filter)]
    if not candidates:
        return {
            "status": "unchanged",
            **empty,
            "guidance": "No triggered alarm matches the filter. MEHO sent no reset.",
        }

    gate, _ = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_CLEAR_TRIGGERED_ALARMS,
        vmomi_path=f"/AlarmManager/{alarm_manager}/ClearTriggeredAlarms",
        body={"filter": _filter_spec(reset_filter)},
        params=dict(reset_filter),
    )
    if gate is not None:
        return gate

    try:
        after = await _root_alarms(connector, target, operator, root_folder, with_names=False)
    except (httpx.HTTPError, RuntimeError) as exc:
        return _error(
            "not_verified",
            "MEHO sent the reset, but could not read the alarms again "
            f"({type(exc).__name__}); what was cleared is not confirmed",
            "Call vmware.alarm.triggered.list to see which alarms are still triggered.",
            filter=reset_filter,
            cleared=None,
            still_triggered=None,
        )
    return _reset_outcome(reset_filter, before, candidates, after)


async def alarm_reset_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview for the approver: the filter and the triggered alarms it matches now.

    Lists at most :data:`_PREVIEW_CAP` matching alarms; ``total_matching`` has
    the full count. Declines (``None``) without a connector, as in
    ``preview_operation``: the matching alarms can only be shown after a read
    of vCenter.
    """
    try:
        reset_filter = _reset_filter(ctx.params)
        _filter_spec(reset_filter)
    except (KeyError, TypeError):
        return None
    if ctx.connector_instance is None:
        return None
    preview: dict[str, Any] = {
        "action": "reset_alarms_to_green",
        "filter": reset_filter,
        "note": (
            "vCenter resets every triggered alarm that matches the filter, not only one. "
            "An alarm that is reset hides its problem until the alarm fires again."
        ),
    }
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    root_folder, _ = await read_service_content_refs(connector, ctx.target, ctx.operator)
    rows = await _root_alarms(connector, ctx.target, ctx.operator, root_folder)
    matching = [row for row in rows if _matches(row, reset_filter)]
    preview["matching_now"] = matching[:_PREVIEW_CAP]
    preview["total_matching"] = len(matching)
    return preview
