# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""ABX actions and event subscriptions of Aria Automation 8 and VCF Automation 9.

Written once and registered on both connectors, like
:mod:`.vra_governance` (which also holds :class:`.VraGovCalls`).

Running an ABX action and changing an event subscription is
``destructive``: an ABX action is a script that can do anything, and a
subscription runs an ABX action or a workflow on future events. So each
change has a preview builder. It reads the object first, fills the
``blast_radius`` block, and refuses (raises) when the object does not
exist. When the approved call runs, the handler reads the object again and
stops with ``precondition_failed`` when it is no longer what the approver
saw. The card holds a SHA-256 over every field that decides what runs (for
an action: its code and settings; for a subscription: its topic, runnable,
filters and flags), so any change after the approval stops the call. The
card holds only the hash, never the code.

Secrets:

* The source code of an action (``source``, ``compressedContent``) and its
  ``configuration`` are never returned; the input defaults neither, only
  the input names.
* A run input that holds a secret is refused: a secret-named key at any
  depth (also inside an object or a list), or a text that holds a
  secret-named pair. A script gets its secrets from Automation itself
  (action constants), never from MEHO.
* The inputs, outputs, error text and log of a run go through the
  secret-name scrub (:func:`~meho_backplane.redaction.secret_fields.scrub_secret_fields`).

Where the paths come from: the vendor's published ABX and Event Broker API
references. No pinned API file holds them; the reconcile lane pins the calls.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Final

import httpx

from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.connectors.vmware_shared.vra_governance import (
    VraChangeRefusedError,
    VraGovCalls,
    content_rows,
    path_segment,
)
from meho_backplane.operations._preview import (
    PreviewContext,
    approved_preview,
    register_preview_builder,
)
from meho_backplane.redaction.secret_fields import (
    scrub_secret_fields,
    scrub_secret_fields_in_text,
)

__all__ = [
    "ABX_ACTIONS_PATH",
    "ABX_ACTION_PATH",
    "ABX_ACTION_RUNS_PATH",
    "ABX_RUN_PATH",
    "ABX_RUN_STATES",
    "EVENT_TOPIC_PATH",
    "SUBSCRIPTIONS_PATH",
    "SUBSCRIPTION_PATH",
    "abx_action_list",
    "abx_action_run",
    "abx_run_get",
    "register_extensibility_previews",
    "subscription_list",
    "subscription_row",
    "subscription_set",
]

ABX_ACTIONS_PATH: Final[str] = "/abx/api/resources/actions"
ABX_ACTION_PATH: Final[str] = "/abx/api/resources/actions/{id}"
ABX_ACTION_RUNS_PATH: Final[str] = "/abx/api/resources/actions/{id}/action-runs"
ABX_RUN_PATH: Final[str] = "/abx/api/resources/action-runs/{id}"
SUBSCRIPTIONS_PATH: Final[str] = "/event-broker/api/subscriptions"
SUBSCRIPTION_PATH: Final[str] = "/event-broker/api/subscriptions/{id}"
EVENT_TOPIC_PATH: Final[str] = "/event-broker/api/topics/{id}"

#: The run states the vendor documents (``ActionRun.runState``).
ABX_RUN_STATES: Final[tuple[str, ...]] = (
    "CREATED",
    "DEPLOYING",
    "DEPLOYED",
    "RUNNING",
    "FINISHING",
    "HANDLING_ERROR",
    "COMPLETED",
    "DEPLOYMENT_FAILED",
    "FAILED",
    "CANCELLED",
    "UNKNOWN",
)
_RUN_DONE: Final[frozenset[str]] = frozenset(
    {"COMPLETED", "DEPLOYMENT_FAILED", "FAILED", "CANCELLED"}
)

#: How many pages of subscriptions the name check reads at most.
_NAME_CHECK_PAGES: Final[int] = 20
_NAME_CHECK_PAGE_SIZE: Final[int] = 100


# ---------------------------------------------------------------------------
# ABX actions
# ---------------------------------------------------------------------------


def _input_names(inputs: Any) -> list[str]:
    return sorted(str(name) for name in inputs) if isinstance(inputs, Mapping) else []


def _action_row(action: Mapping[str, Any]) -> dict[str, Any]:
    """An action without its source, configuration and input defaults."""
    return {
        "id": action.get("id"),
        "name": action.get("name"),
        "description": action.get("description"),
        "actionType": action.get("actionType"),
        "runtime": action.get("runtime"),
        "runtimeVersion": action.get("runtimeVersion"),
        "provider": action.get("provider"),
        "projectId": action.get("projectId"),
        "orgId": action.get("orgId"),
        "entrypoint": action.get("entrypoint"),
        "timeoutSeconds": action.get("timeoutSeconds"),
        "memoryInMB": action.get("memoryInMB"),
        "shared": action.get("shared"),
        "system": action.get("system"),
        "inputNames": _input_names(action.get("inputs")),
    }


async def abx_action_list(calls: VraGovCalls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<prefix>.abx.action.list`` — ``GET /abx/api/resources/actions`` as flat rows."""
    query = {
        wire: params[name]
        for name, wire in (("page", "page"), ("size", "size"), ("name", "nameCriteria"))
        if params.get(name) is not None
    }
    page = await calls.get(ABX_ACTIONS_PATH, query or None)
    return {
        "actions": [_action_row(action) for action in content_rows(page)],
        "totalElements": page.get("totalElements"),
    }


def _secret_input_paths(node: Any, trail: str) -> list[str]:
    """The paths in *node* that hold a secret, at any depth.

    A key with a secret name (``password``, ``apiKey``, ...) is one, inside
    an object or a list as much as at the top. So is a string that holds a
    secret-named pair, such as a JSON document passed as text.
    """
    found: list[str] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            path = f"{trail}.{key}" if trail else str(key)
            if is_secret_param_name(str(key)):
                found.append(path)
            found.extend(_secret_input_paths(value, path))
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            found.extend(_secret_input_paths(value, f"{trail}[{index}]"))
    elif isinstance(node, str) and scrub_secret_fields_in_text(node) != node:
        found.append(trail)
    return found


def _secret_input_problem(inputs: Mapping[str, Any]) -> str | None:
    named = sorted(set(_secret_input_paths(inputs, "")))
    if not named:
        return None
    return (
        f"inputs {named} look like secrets. MEHO does not pass secrets to a script; "
        "store them in Automation as action constants (secrets) and read them in the script"
    )


async def _read_action(
    calls: VraGovCalls, action_id: str, project_id: str | None
) -> dict[str, Any]:
    """The action, or :exc:`VraChangeRefusedError` when it does not exist."""
    path = ABX_ACTION_PATH.format(id=path_segment(action_id, "id"))
    try:
        return await calls.get(path, {"projectId": project_id} if project_id else None)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (403, 404):
            raise VraChangeRefusedError(
                f"Automation found no ABX action with id {action_id!r} that this login "
                f"may read (HTTP {exc.response.status_code}); list the actions with "
                f"{calls.prefix}.abx.action.list"
            ) from exc
        raise


#: The action fields that decide what a run does. The approval card pins a
#: hash over them (never the values: ``source`` is code, ``configuration``
#: and the input defaults can hold secrets).
_ACTION_PINNED_FIELDS: Final[tuple[str, ...]] = (
    "source",
    "compressedContent",
    "contentId",
    "entrypoint",
    "dependencies",
    "runtime",
    "runtimeVersion",
    "actionType",
    "provider",
    "configuration",
    "inputs",
    "timeoutSeconds",
    "memoryInMB",
)

#: The subscription fields that decide when it fires and what it runs.
_SUBSCRIPTION_PINNED_FIELDS: Final[tuple[str, ...]] = (
    "name",
    "eventTopicId",
    "runnableType",
    "runnableId",
    "blocking",
    "criteria",
    "constraints",
    "disabled",
    "priority",
    "timeout",
)


def _pinned_sha256(source: Mapping[str, Any], fields: tuple[str, ...]) -> str:
    """SHA-256 over the canonical JSON of *fields* of *source*."""
    picked = {field: source.get(field) for field in fields}
    canonical = json.dumps(picked, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _action_card(action: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "blast_radius": {
            "object": {
                "kind": "abx_action",
                "id": action.get("id"),
                "name": action.get("name"),
                "runtime": action.get("runtime"),
                "action_type": action.get("actionType"),
                "provider": action.get("provider"),
                "project_id": params.get("project_id") or action.get("projectId"),
                "content_id": action.get("contentId"),
                "input_names": _input_names(params.get("inputs") or {}),
                "code_sha256": _pinned_sha256(action, _ACTION_PINNED_FIELDS),
            },
            "children": [
                {
                    "kind": "script_effects",
                    "note": (
                        "the script can change any system its code and its secrets reach; "
                        "MEHO cannot list them"
                    ),
                }
            ],
            "irreversibility": "unknown: the script may make changes that cannot be undone",
        }
    }


#: Fields of the approved card that must still match when the run starts.
_ACTION_SEEN_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("name", "name"),
    ("runtime", "runtime"),
    ("content_id", "contentId"),
)


def _changed_since_approval(
    seen: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    fields: tuple[tuple[str, str], ...],
    pin: tuple[str, tuple[str, ...]],
) -> str | None:
    """Why *current* is not what the approver saw, or ``None`` when it is.

    *seen* is ``None`` on an ordinary dispatch (no approval to compare
    with). An approved card without the object block or without the pinned
    hash (*pin*: card key and the vendor fields it covers) fails closed.
    """
    if seen is None:
        return None
    block = seen.get("blast_radius") if isinstance(seen, Mapping) else None
    obj = block.get("object") if isinstance(block, Mapping) else None
    pin_key, pin_fields = pin
    if not isinstance(obj, Mapping) or not obj.get(pin_key):
        return (
            "the approval card does not hold what the approver saw; nothing was changed. "
            "Preview and ask again."
        )
    for card_key, vendor_key in fields:
        if obj.get(card_key) != current.get(vendor_key):
            return (
                f"{card_key} was {obj.get(card_key)!r} when the call was approved and is "
                f"{current.get(vendor_key)!r} now; nothing was changed. Preview and ask again."
            )
    if obj.get(pin_key) != _pinned_sha256(current, pin_fields):
        return (
            f"{pin_key} differs: the code or settings changed after the call was approved; "
            "nothing was changed. Preview and ask again."
        )
    return None


async def abx_action_run(calls: VraGovCalls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<prefix>.abx.action.run`` — ``POST /abx/api/resources/actions/{id}/action-runs``."""
    inputs = dict(params.get("inputs") or {})
    problem = _secret_input_problem(inputs)
    if problem is not None:
        return {"status": "invalid_request", "run": None, "guidance": problem}
    action_id = str(params["id"])
    action = await _read_action(calls, action_id, params.get("project_id"))
    changed = _changed_since_approval(
        approved_preview(f"{calls.prefix}.abx.action.run"),
        action,
        _ACTION_SEEN_FIELDS,
        ("code_sha256", _ACTION_PINNED_FIELDS),
    )
    if changed is not None:
        return {"status": "precondition_failed", "run": None, "guidance": changed}
    body: dict[str, Any] = {"inputs": inputs}
    project = params.get("project_id") or action.get("projectId")
    if project:
        body["projectId"] = project
    path = ABX_ACTION_RUNS_PATH.format(id=path_segment(action_id, "id"))
    run = await calls.send("POST", path, None, body)
    run = run if isinstance(run, Mapping) else {}
    return {
        "status": "started",
        "run": {
            "id": run.get("id"),
            "actionId": run.get("actionId") or action_id,
            "name": run.get("name") or action.get("name"),
            "runState": run.get("runState"),
            "projectId": run.get("projectId") or project,
        },
        "guidance": (
            f"read {calls.prefix}.abx.run.get with run.id until done is true "
            "(runState COMPLETED, FAILED, DEPLOYMENT_FAILED or CANCELLED)"
        ),
    }


async def abx_run_get(calls: VraGovCalls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<prefix>.abx.run.get`` — ``GET /abx/api/resources/action-runs/{id}``."""
    run = await calls.get(ABX_RUN_PATH.format(id=path_segment(params["id"], "id")), None)
    logs = run.get("logs")
    lines = scrub_secret_fields(logs).splitlines() if isinstance(logs, str) else []
    tail = int(params.get("log_tail") or 200)
    state = run.get("runState")
    result: dict[str, Any] = scrub_secret_fields(
        {
            "id": run.get("id"),
            "actionId": run.get("actionId"),
            "name": run.get("name"),
            "runState": state,
            "done": state in _RUN_DONE,
            "errorMessage": run.get("errorMessage"),
            "actionType": run.get("actionType"),
            "runtime": run.get("runtime"),
            "projectId": run.get("projectId"),
            "createdMillis": run.get("createdMillis"),
            "startTimeMillis": run.get("startTimeMillis"),
            "endTimeMillis": run.get("endTimeMillis"),
            "inputs": run.get("inputs"),
            "outputs": run.get("outputs"),
            "log_lines_total": len(lines),
            "log_lines": lines[-tail:],
        }
    )
    return result


# ---------------------------------------------------------------------------
# Event subscriptions
# ---------------------------------------------------------------------------


def subscription_row(sub: Mapping[str, Any]) -> dict[str, Any]:
    """One subscription as a flat row."""
    constraints = sub.get("constraints")
    projects = constraints.get("projectId") if isinstance(constraints, Mapping) else None
    row = {
        "id": sub.get("id"),
        "name": sub.get("name"),
        "description": sub.get("description"),
        "eventTopicId": sub.get("eventTopicId"),
        "runnableType": sub.get("runnableType"),
        "runnableId": sub.get("runnableId"),
        "blocking": sub.get("blocking"),
        "priority": sub.get("priority"),
        "timeout": sub.get("timeout"),
        "disabled": sub.get("disabled"),
        "criteria": sub.get("criteria"),
        "projectIds": projects if isinstance(projects, list) else [],
        "system": sub.get("system"),
        "ownerId": sub.get("ownerId"),
    }
    cleaned: dict[str, Any] = scrub_secret_fields(row)
    return cleaned


async def subscription_list(calls: VraGovCalls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<prefix>.subscription.list`` — ``GET /event-broker/api/subscriptions`` as flat rows."""
    query = {name: params[name] for name in ("page", "size") if params.get(name) is not None}
    page = await calls.get(SUBSCRIPTIONS_PATH, query or None)
    return {
        "subscriptions": [subscription_row(sub) for sub in content_rows(page)],
        "totalElements": page.get("totalElements"),
    }


#: Param name -> vendor field, for the fields a create or an update sets.
_SUBSCRIPTION_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("name", "name"),
    ("description", "description"),
    ("event_topic_id", "eventTopicId"),
    ("runnable_type", "runnableType"),
    ("runnable_id", "runnableId"),
    ("blocking", "blocking"),
    ("priority", "priority"),
    ("timeout", "timeout"),
    ("criteria", "criteria"),
)

#: Fields of the approved card that must still match when the change runs.
_SUBSCRIPTION_SEEN_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("name", "name"),
    ("event_topic_id", "eventTopicId"),
    ("runnable_id", "runnableId"),
)


def _wanted_fields(params: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        wire: params[name] for name, wire in _SUBSCRIPTION_FIELDS if params.get(name) is not None
    }
    if params.get("project_ids") is not None:
        fields["constraints"] = {"projectId": list(params["project_ids"])}
    return fields


async def _read_subscription(calls: VraGovCalls, sub_id: str) -> dict[str, Any] | None:
    path = SUBSCRIPTION_PATH.format(id=path_segment(sub_id, "id"))
    try:
        return await calls.get(path, None)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (403, 404):
            return None
        raise


async def _subscription_by_name(calls: VraGovCalls, name: str) -> dict[str, Any] | None:
    for number in range(_NAME_CHECK_PAGES):
        page = await calls.get(SUBSCRIPTIONS_PATH, {"page": number, "size": _NAME_CHECK_PAGE_SIZE})
        rows = content_rows(page)
        for row in rows:
            if row.get("name") == name:
                return row
        if not rows or page.get("last") is not False:
            return None
    return None


def _set_answer(status: str, sub: Mapping[str, Any] | None, guidance: str) -> dict[str, Any]:
    return {
        "status": status,
        "subscription": subscription_row(sub) if sub is not None else None,
        "guidance": guidance,
    }


async def _create_subscription(calls: VraGovCalls, params: Mapping[str, Any]) -> dict[str, Any]:
    existing = await _subscription_by_name(calls, str(params["name"]))
    if existing is not None:
        return _set_answer(
            "unchanged",
            existing,
            "a subscription with this name exists; nothing was made. Change it with "
            "action update and its id.",
        )
    body = {"type": "RUNNABLE", "disabled": bool(params.get("disabled", False))}
    body.update(_wanted_fields(params))
    made = await calls.send("POST", SUBSCRIPTIONS_PATH, None, body)
    return _set_answer(
        "created",
        made if isinstance(made, Mapping) and made.get("id") else body,
        f"the subscription was made; check it with {calls.prefix}.subscription.list",
    )


def _differs(current: Mapping[str, Any], wanted: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in wanted.items() if current.get(key) != value}


async def _change_subscription(
    calls: VraGovCalls, action: str, current: Mapping[str, Any], params: Mapping[str, Any]
) -> dict[str, Any]:
    path = SUBSCRIPTION_PATH.format(id=path_segment(params["id"], "id"))
    if action == "delete":
        await calls.send("DELETE", path, None, None)
        return _set_answer("deleted", current, "the subscription was deleted")
    wanted = _wanted_fields(params) if action == "update" else {"disabled": action == "disable"}
    patch = _differs(current, wanted)
    if not patch:
        return _set_answer("unchanged", current, "the subscription already has these values")
    changed = await calls.send("PATCH", path, None, patch)
    after = changed if isinstance(changed, Mapping) and changed.get("id") else {**current, **patch}
    status = {"update": "changed", "disable": "disabled", "enable": "enabled"}[action]
    return _set_answer(status, after, f"done; check it with {calls.prefix}.subscription.list")


async def subscription_set(calls: VraGovCalls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<prefix>.subscription.set`` — create, change, turn off or on, or delete one."""
    action = str(params["action"])
    if action == "create":
        return await _create_subscription(calls, params)
    current = await _read_subscription(calls, str(params["id"]))
    if current is None:
        status = "unchanged" if action == "delete" else "invalid_request"
        return _set_answer(status, None, f"no subscription with id {params['id']!r}")
    if current.get("system") is True:
        return _set_answer("invalid_request", current, "a system subscription is not changed")
    changed = _changed_since_approval(
        approved_preview(f"{calls.prefix}.subscription.set"),
        current,
        _SUBSCRIPTION_SEEN_FIELDS,
        ("body_sha256", _SUBSCRIPTION_PINNED_FIELDS),
    )
    if changed is not None:
        return _set_answer("precondition_failed", current, changed)
    return await _change_subscription(calls, action, current, params)


# ---------------------------------------------------------------------------
# Preview builders (the approval card of each destructive change)
# ---------------------------------------------------------------------------


def _calls_for(ctx: PreviewContext) -> VraGovCalls | None:
    factory = getattr(ctx.connector_instance, "vra_gov_calls", None)
    if factory is None or ctx.target is None:
        return None
    calls: VraGovCalls = factory(ctx.operator, ctx.target)
    return calls


async def _abx_run_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Read the action and show what the run starts. Raises to refuse."""
    calls = _calls_for(ctx)
    if calls is None:
        return None
    problem = _secret_input_problem(dict(ctx.params.get("inputs") or {}))
    if problem is not None:
        raise VraChangeRefusedError(problem)
    action = await _read_action(calls, str(ctx.params.get("id", "")), ctx.params.get("project_id"))
    return _action_card(action, ctx.params)


async def _runnable_child(calls: VraGovCalls, params: Mapping[str, Any]) -> dict[str, Any]:
    """The script the subscription runs; for an ABX action, read to check it exists."""
    child = {
        "kind": "runnable",
        "type": params.get("runnable_type"),
        "id": params.get("runnable_id"),
    }
    if params.get("runnable_type") == "extensibility.abx" and params.get("runnable_id"):
        action = await _read_action(calls, str(params["runnable_id"]), None)
        child["name"] = action.get("name")
    return child


async def _topic_child(calls: VraGovCalls, topic_id: str) -> dict[str, Any]:
    path = EVENT_TOPIC_PATH.format(id=path_segment(topic_id, "event_topic_id"))
    try:
        topic = await calls.get(path, None)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (403, 404):
            raise VraChangeRefusedError(
                f"Automation found no event topic {topic_id!r} (HTTP {exc.response.status_code})"
            ) from exc
        raise
    return {
        "kind": "event_topic",
        "id": topic_id,
        "name": topic.get("name"),
        "blockable": topic.get("blockable"),
    }


_IRREVERSIBILITY: Final[dict[str, str]] = {
    "create": "reversible: delete the subscription to stop it; what its runs did stays",
    "update": "reversible: set the old values again (shown in object)",
    "disable": "reversible: turn it on again with action enable",
    "enable": "reversible: turn it off again with action disable; what its runs did stays",
    "delete": "permanent: the subscription is gone; make it again to restore it",
}


async def _subscription_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Read the subscription (or check the new one) and show the change. Raises to refuse."""
    calls = _calls_for(ctx)
    if calls is None:
        return None
    params = ctx.params
    action = str(params.get("action", ""))
    if action == "create":
        name = str(params.get("name", ""))
        if await _subscription_by_name(calls, name) is not None:
            raise VraChangeRefusedError(
                f"a subscription named {name!r} exists; change it with action update and its id"
            )
        new_row = subscription_row(_params_as_wire(params))
        obj: dict[str, Any] = {"kind": "event_subscription", **new_row}
        topic_id = str(params.get("event_topic_id", ""))
        children = [await _topic_child(calls, topic_id), await _runnable_child(calls, params)]
    else:
        sub_id = str(params.get("id", ""))
        current = await _read_subscription(calls, sub_id)
        if current is None:
            raise VraChangeRefusedError(
                f"Automation found no event subscription with id {sub_id!r} that this login "
                f"may read; list them with {calls.prefix}.subscription.list"
            )
        if current.get("system") is True:
            raise VraChangeRefusedError(f"subscription {sub_id!r} is a system subscription")
        row = subscription_row(current)
        obj = {
            "kind": "event_subscription",
            **row,
            "event_topic_id": current.get("eventTopicId"),
            "runnable_id": current.get("runnableId"),
            "body_sha256": _pinned_sha256(current, _SUBSCRIPTION_PINNED_FIELDS),
        }
        if action == "update":
            obj["requested_change"] = _differs(current, _wanted_fields(params))
        children = [
            {"kind": "event_topic", "id": current.get("eventTopicId")},
            {
                "kind": "runnable",
                "type": current.get("runnableType"),
                "id": current.get("runnableId"),
            },
        ]
    return {
        "blast_radius": {
            "object": obj,
            "children": children,
            "irreversibility": _IRREVERSIBILITY.get(action, "unknown"),
        }
    }


def _params_as_wire(params: Mapping[str, Any]) -> dict[str, Any]:
    """The vendor-shaped subscription a create would send (for the approval card)."""
    return {"disabled": bool(params.get("disabled", False)), **_wanted_fields(params)}


def register_extensibility_previews(prefix: str) -> None:
    """Register the approval-card builders of the two destructive ops for *prefix*."""
    register_preview_builder(f"{prefix}.abx.action.run", _abx_run_preview)
    register_preview_builder(f"{prefix}.subscription.set", _subscription_preview)
