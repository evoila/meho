# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Give a running realm the approval sign-in flow: ``keycloak.authflow.ensure_approval_flow``.

The chart's realm file defines the login flow ``meho approval sign-in level``
and binds it to the client ``meho-cli-approve``. Keycloak reads that file
only when the realm does not exist yet, so a realm made earlier has no such
flow. This op adds it with one governed request.

The flow gives the approval level (#6281): level 2 of the realm's sign-in
levels, named ``meho-approve`` in the ``acr`` claim. No other flow of the
realm gives this level, so MEHO can tell an approval sign-in from any other
sign-in.

What the op does, on the target's managed realm:

1. Find the client (default ``meho-cli-approve``). No client: stop before
   anything is written.
2. Find the flow ``meho approval sign-in level``.

   * It does not exist: make it with the same steps as the realm file. One
     conditional sub-flow ``meho approval sign-in level - level 2`` with the
     level step ``conditional-level-of-authentication`` (level 2, maximum
     age 0, so it is asked for at every sign-in), the password form, and the
     conditional sub-flow ``meho approval sign-in level - one-time code``
     (``conditional-user-configured`` and ``auth-otp-form``). Then read the
     steps again and check them.
   * It exists with the same steps: change nothing, except that a level step
     without its settings gets them (a run that stopped half-way).
   * It exists with other steps, or the level step has other settings: stop
     and name the first difference. Nothing is written.

3. Name the level: the realm attribute ``acr.loa.map`` gets
   ``"meho-approve": 2``, next to the names it has.
4. Bind the flow as the client's browser flow
   (``authenticationFlowBindingOverrides.browser``), unless it already is.
   The client is read, the one key is put on top, and the full client is
   written back, so no other client setting changes. Then the client is read
   again; the result reports the browser flow id that was read back.

Safe to run twice: a second run finds the flow, the level and the binding and
writes nothing. Risk level ``caution`` with approval: the op changes how
people sign in to approve.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.keycloak._paths import (
    _ADMIN_REALM_PATH,
    _AUTH_CONFIG_PATH,
    _AUTH_EXECUTION_CONFIG_PATH,
    _AUTH_FLOW_ADD_STEP_PATH,
    _AUTH_FLOW_ADD_SUBFLOW_PATH,
    _AUTH_FLOW_EXECUTIONS_PATH,
    _AUTH_FLOWS_PATH,
    _CLIENT_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_authflow_schemas import (
    APPROVAL_FLOW_OPS,
    ENSURE_APPROVAL_FLOW_OP_ID,
    WHEN_TO_USE_AUTHFLOW_BY_GROUP,
)
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import (
    KeycloakUnexpectedResponseError,
)
from meho_backplane.connectors.keycloak.ops_write_merge import merge_representation
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "APPROVAL_FLOW_ALIAS",
    "APPROVAL_FLOW_OPS",
    "ENSURE_APPROVAL_FLOW_OP_ID",
    "WHEN_TO_USE_AUTHFLOW_BY_GROUP",
    "KeycloakApprovalFlowError",
    "approval_flow_difference",
    "get_admin_list_strict",
    "keycloak_authflow_ensure_approval_flow",
]

APPROVAL_FLOW_ALIAS = "meho approval sign-in level"
_LEVEL_FLOW_ALIAS = "meho approval sign-in level - level 2"
_CODE_FLOW_ALIAS = "meho approval sign-in level - one-time code"
_DEFAULT_CLIENT_ID = "meho-cli-approve"
_FLOW_DESCRIPTION = (
    "Sign-in of `meho approvals approve`. It gives the approval level 2 (acr "
    "meho-approve) and nothing else gives it: the password form, then the one-time "
    "code when the person has set one up, every time."
)
_SUBFLOW_DESCRIPTIONS = {
    _LEVEL_FLOW_ALIAS: (
        "Level 2: the password, then the one-time code. Asked for again at every sign-in."
    ),
    _CODE_FLOW_ALIAS: "Asks for the one-time code when the person has set one up.",
}
#: The realm attribute that names sign-in levels, and the name of the approval level.
LEVEL_MAP_ATTRIBUTE = "acr.loa.map"
APPROVAL_LEVEL_NAME = "meho-approve"
APPROVAL_LEVEL = 2
#: The settings of the level step: level 2, asked for at every sign-in.
_LEVEL_CONFIG_ALIAS = _LEVEL_FLOW_ALIAS
_LEVEL_CONFIG = {"loa-condition-level": str(APPROVAL_LEVEL), "loa-max-age": "0"}
_LEVEL_STEP = "conditional-level-of-authentication"


@dataclass(frozen=True)
class _Step:
    """One step of the approval flow, as Keycloak lists it."""

    level: int
    name: str
    requirement: str
    parent: str
    subflow: bool = False


#: The steps in the order Keycloak lists them (a sub-flow's steps follow it).
_STEPS: tuple[_Step, ...] = (
    _Step(0, _LEVEL_FLOW_ALIAS, "CONDITIONAL", APPROVAL_FLOW_ALIAS, subflow=True),
    _Step(1, _LEVEL_STEP, "REQUIRED", _LEVEL_FLOW_ALIAS),
    _Step(1, "auth-username-password-form", "REQUIRED", _LEVEL_FLOW_ALIAS),
    _Step(1, _CODE_FLOW_ALIAS, "CONDITIONAL", _LEVEL_FLOW_ALIAS, subflow=True),
    _Step(2, "conditional-user-configured", "REQUIRED", _CODE_FLOW_ALIAS),
    _Step(2, "auth-otp-form", "REQUIRED", _CODE_FLOW_ALIAS),
)


class KeycloakApprovalFlowError(RuntimeError):
    """The approval flow op stopped. ``code`` is a short, stable word.

    ``client_not_found``: the client does not exist. ``other_steps``: a flow
    with the name exists with other steps, or its level step has other
    settings. ``levels_not_object``: the realm's ``acr.loa.map`` is not a
    JSON object. ``check_failed``: the flow or the
    binding did not read back as written. ``flow_not_found``: the new flow
    could not be found after it was made.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{ENSURE_APPROVAL_FLOW_OP_ID} stopped ({code}): {message}")
        self.code = code


async def get_admin_list_strict(
    connector: KeycloakConnector,
    target: KeycloakTargetLike,
    path: str,
    *,
    operator: Operator,
    params: dict[str, Any] | None = None,
) -> list[Any]:
    """GET a JSON array. Any other body raises; it is never read as an empty list."""
    client = await connector._http_client(target)
    headers = await connector.auth_headers(target, operator)
    resp = await client.request("GET", path, params=params, headers=headers)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list):
        raise KeycloakUnexpectedResponseError(
            f"GET {path} answered with {type(payload).__name__}, not a JSON array"
        )
    return payload


def _step_name(row: dict[str, Any]) -> str:
    """A sub-flow is named by its alias (``displayName``), a step by its provider."""
    key = "displayName" if row.get("authenticationFlow") is True else "providerId"
    value = row.get(key)
    return value if isinstance(value, str) else ""


def _listed_steps(rows: list[Any]) -> list[dict[str, Any]]:
    """The steps of a flow as ``{level, step, requirement}``."""
    steps = []
    for row in rows:
        if not isinstance(row, dict):
            raise KeycloakUnexpectedResponseError("a flow step is not a JSON object")
        steps.append(
            {
                "level": row.get("level"),
                "step": _step_name(row),
                "requirement": row.get("requirement"),
            }
        )
    return steps


def _difference(steps: list[dict[str, Any]]) -> str | None:
    """Name the first difference to the wanted steps, or ``None`` when they match."""
    for index, want in enumerate(_STEPS):
        if index >= len(steps):
            return f"step {index + 1} ({want.name!r}) is missing"
        have = steps[index]
        if (have["level"], have["step"], have["requirement"]) != (
            want.level,
            want.name,
            want.requirement,
        ):
            return (
                f"step {index + 1} is {have['step']!r} ({have['requirement']}, level "
                f"{have['level']}); wanted {want.name!r} ({want.requirement}, level {want.level})"
            )
    if len(steps) > len(_STEPS):
        return f"the flow has {len(steps)} steps; wanted {len(_STEPS)}"
    return None


def approval_flow_difference(rows: list[Any]) -> str | None:
    """The first difference of the listed steps *rows* to the approval flow.

    *rows* is the answer of ``GET .../authentication/flows/<alias>/executions``.
    ``None`` when the steps are the ones of the realm file. The health check
    uses it too (``meho_backplane.auth.approval_sign_in_flow``).
    """
    return _difference(_listed_steps(rows))


async def _read_steps(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    alias: str,
) -> list[Any]:
    path = fill_path(
        _AUTH_FLOW_EXECUTIONS_PATH, {"realm": realm, "flowAlias": quote_segment(alias)}
    )
    return await get_admin_list_strict(connector, target, path, operator=operator)


async def _find_flow_id(
    connector: KeycloakConnector, operator: Operator, target: KeycloakTargetLike, realm: str
) -> str | None:
    flows = await get_admin_list_strict(
        connector, target, fill_path(_AUTH_FLOWS_PATH, {"realm": realm}), operator=operator
    )
    for flow in flows:
        if isinstance(flow, dict) and flow.get("alias") == APPROVAL_FLOW_ALIAS:
            flow_id = flow.get("id")
            if isinstance(flow_id, str) and flow_id:
                return flow_id
    return None


async def _make_flow(
    connector: KeycloakConnector, operator: Operator, target: KeycloakTargetLike, realm: str
) -> str:
    """Make the flow and its steps, set each requirement, and return the flow id."""
    await connector._write_admin(
        target,
        "POST",
        fill_path(_AUTH_FLOWS_PATH, {"realm": realm}),
        operator=operator,
        json={
            "alias": APPROVAL_FLOW_ALIAS,
            "description": _FLOW_DESCRIPTION,
            "providerId": "basic-flow",
            "topLevel": True,
            "builtIn": False,
        },
        idempotent_conflict=False,
    )
    for step in _STEPS:
        values = {"realm": realm, "flowAlias": quote_segment(step.parent)}
        if step.subflow:
            path = fill_path(_AUTH_FLOW_ADD_SUBFLOW_PATH, values)
            body = {
                "alias": step.name,
                "type": "basic-flow",
                "description": _SUBFLOW_DESCRIPTIONS[step.name],
            }
        else:
            path = fill_path(_AUTH_FLOW_ADD_STEP_PATH, values)
            body = {"provider": step.name}
        await connector._write_admin(
            target, "POST", path, operator=operator, json=body, idempotent_conflict=False
        )
    # Keycloak makes every new step DISABLED; set the wanted requirement.
    wanted: dict[tuple[Any, str], str] = {
        (step.level, step.name): step.requirement for step in _STEPS
    }
    executions_path = fill_path(
        _AUTH_FLOW_EXECUTIONS_PATH,
        {"realm": realm, "flowAlias": quote_segment(APPROVAL_FLOW_ALIAS)},
    )
    for row in await _read_steps(connector, operator, target, realm, APPROVAL_FLOW_ALIAS):
        if not isinstance(row, dict):
            continue
        requirement = wanted.get((row.get("level"), _step_name(row)))
        if requirement is None or row.get("requirement") == requirement:
            continue
        await connector._write_admin(
            target,
            "PUT",
            executions_path,
            operator=operator,
            json={**row, "requirement": requirement},
            idempotent_conflict=False,
        )
    flow_id = await _find_flow_id(connector, operator, target, realm)
    if flow_id is None:
        raise KeycloakApprovalFlowError(
            "flow_not_found", f"flow {APPROVAL_FLOW_ALIAS!r} was made but cannot be found"
        )
    return flow_id


async def _ensure_level_setting(
    connector: KeycloakConnector, operator: Operator, target: KeycloakTargetLike, realm: str
) -> bool:
    """Give the level step its settings when it has none; whether it was written.

    Settings with other values are not changed: the op stops with
    ``other_steps``. Runs on a flow whose steps match :data:`_STEPS`.
    """
    rows = await _read_steps(connector, operator, target, realm, APPROVAL_FLOW_ALIAS)
    step = next(row for row in rows if isinstance(row, dict) and _step_name(row) == _LEVEL_STEP)
    config_id = step.get("authenticationConfig")
    if config_id:
        path = fill_path(_AUTH_CONFIG_PATH, {"realm": realm, "id": quote_segment(str(config_id))})
        have = await connector._get_admin_json(target, path, operator=operator)
        config = have.get("config")
        values: dict[str, Any] = config if isinstance(config, dict) else {}
        if any(values.get(key) != value for key, value in _LEVEL_CONFIG.items()):
            raise KeycloakApprovalFlowError(
                "other_steps",
                f"the level step of flow {APPROVAL_FLOW_ALIAS!r} has the settings {values!r}; "
                f"wanted {_LEVEL_CONFIG!r}. Nothing was changed.",
            )
        return False
    path = fill_path(
        _AUTH_EXECUTION_CONFIG_PATH,
        {"realm": realm, "executionId": quote_segment(str(step.get("id")))},
    )
    await connector._write_admin(
        target,
        "POST",
        path,
        operator=operator,
        json={"alias": _LEVEL_CONFIG_ALIAS, "config": dict(_LEVEL_CONFIG)},
        idempotent_conflict=False,
    )
    return True


async def _ensure_level_name(
    connector: KeycloakConnector, operator: Operator, target: KeycloakTargetLike, realm: str
) -> bool:
    """Name level 2 ``meho-approve`` in the realm's ``acr.loa.map``; whether it was written.

    The other names of the map stay. The full ``attributes`` map is written
    back, because Keycloak drops realm attributes a write leaves out.
    """
    path = fill_path(_ADMIN_REALM_PATH, {"realm": realm})
    rep = await connector._get_admin_json(target, path, operator=operator)
    attributes = dict(rep.get("attributes") or {})
    try:
        levels = json.loads(attributes.get(LEVEL_MAP_ATTRIBUTE) or "{}")
    except ValueError:
        levels = None
    if not isinstance(levels, dict):
        raise KeycloakApprovalFlowError(
            "levels_not_object",
            f"the realm attribute {LEVEL_MAP_ATTRIBUTE!r} is not a JSON object; "
            "nothing was changed. Fix it in the Keycloak admin console first.",
        )
    if levels.get(APPROVAL_LEVEL_NAME) == APPROVAL_LEVEL:
        return False
    levels[APPROVAL_LEVEL_NAME] = APPROVAL_LEVEL
    attributes[LEVEL_MAP_ATTRIBUTE] = json.dumps(levels, separators=(",", ":"))
    await connector._write_admin(
        target,
        "PUT",
        path,
        operator=operator,
        json={"attributes": attributes},
        idempotent_conflict=False,
    )
    return True


async def _bind(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    client_path: str,
    flow_id: str,
) -> dict[str, Any]:
    """Make *flow_id* the client's browser flow, then read the client again."""
    client = await connector._get_admin_json(target, client_path, operator=operator)
    overrides = client.get("authenticationFlowBindingOverrides")
    previous = overrides.get("browser") if isinstance(overrides, dict) else None
    changed = previous != flow_id
    if changed:
        merged = merge_representation(
            client, {"authenticationFlowBindingOverrides": {"browser": flow_id}}
        )
        await connector._write_admin(
            target, "PUT", client_path, operator=operator, json=merged, idempotent_conflict=False
        )
        client = await connector._get_admin_json(target, client_path, operator=operator)
        overrides = client.get("authenticationFlowBindingOverrides")
    read_back = overrides.get("browser") if isinstance(overrides, dict) else None
    if read_back != flow_id:
        raise KeycloakApprovalFlowError(
            "check_failed",
            f"the client's browser flow reads back as {read_back!r}, not the flow {flow_id!r}",
        )
    return {
        "browser_flow_id": read_back,
        "previous_browser_flow_id": previous,
        "binding_changed": changed,
    }


async def keycloak_authflow_ensure_approval_flow(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Make the approval sign-in flow when it is missing and bind it to the client."""
    realm = resolve_realm_config(target).managed_realm
    realm_segment = quote_segment(realm)
    client_id = str(params.get("client_id") or _DEFAULT_CLIENT_ID).strip()
    client_uuid = await self._find_client_uuid(target, realm_segment, client_id, operator=operator)
    if client_uuid is None:
        raise KeycloakApprovalFlowError(
            "client_not_found", f"no client {client_id!r} in realm {realm!r}; nothing was changed"
        )

    flow_id = await _find_flow_id(self, operator, target, realm_segment)
    created = flow_id is None
    if flow_id is not None:
        steps = _listed_steps(
            await _read_steps(self, operator, target, realm_segment, APPROVAL_FLOW_ALIAS)
        )
        difference = _difference(steps)
        if difference is not None:
            raise KeycloakApprovalFlowError(
                "other_steps",
                f"flow {APPROVAL_FLOW_ALIAS!r} exists with other steps: {difference}. "
                "Nothing was changed. Fix the flow by hand, or delete it and run again.",
            )
    else:
        flow_id = await _make_flow(self, operator, target, realm_segment)
        steps = _listed_steps(
            await _read_steps(self, operator, target, realm_segment, APPROVAL_FLOW_ALIAS)
        )
        difference = _difference(steps)
        if difference is not None:
            raise KeycloakApprovalFlowError(
                "check_failed",
                f"flow {APPROVAL_FLOW_ALIAS!r} was made, but its steps read back wrong: "
                f"{difference}. The client was not bound to it.",
            )

    setting_added = await _ensure_level_setting(self, operator, target, realm_segment)
    level_changed = await _ensure_level_name(self, operator, target, realm_segment)
    client_path = fill_path(
        _CLIENT_PATH, {"realm": realm_segment, "client-uuid": quote_segment(client_uuid)}
    )
    binding = await _bind(self, operator, target, client_path, flow_id)
    return {
        "realm": realm,
        "flow_alias": APPROVAL_FLOW_ALIAS,
        "flow_id": flow_id,
        "flow_created": created,
        "flow_steps": steps,
        "level_setting_added": setting_added,
        "level_name": APPROVAL_LEVEL_NAME,
        "level_changed": level_changed,
        "client_id": client_id,
        "client_uuid": client_uuid,
        **binding,
    }
