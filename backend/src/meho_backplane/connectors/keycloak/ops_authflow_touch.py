# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Give the console the passkey touch level: ``keycloak.authflow.ensure_approval_touch``.

The chart's realm file has the login flow ``meho approval touch`` and binds
it to the console client ``meho-web``. Level 1 of the flow is the normal
login. Level 2 (``acr_values=meho-touch``) asks for a passkey with user
verification, every time (maximum age 0). Keycloak reads that file only
when the realm does not exist yet, so a realm made earlier has none of it.
This op adds it with one governed request.

What the op does, on the target's managed realm:

1. Find the client (default ``meho-web``). No client: stop before anything
   is written.
2. Find the flow ``meho approval touch``.

   * It does not exist: make it with the steps of the realm file, set each
     requirement, and give the four steps with settings their settings
     (the two level conditions, the step that skips the one-time code
     after a passkey, and the passkey step's ``amr`` value with its
     maximum age). Then read the steps again and check them.
   * It exists with the same steps and settings: change nothing.
   * It exists with another shape: stop and name the first difference.
     Nothing is written.

3. Map ``meho-touch`` to level 2 in the realm attribute ``acr.loa.map``,
   next to the names the realm maps already. A realm that maps
   ``meho-touch`` to another level stops the op; the flow is then made but
   not bound.
4. Add the mapper ``amr`` (``oidc-amr-mapper``) to the client, unless it
   has one.
5. Bind the flow as the client's browser flow, unless it already is.

Safe to run twice. Risk level ``caution`` with approval: the op changes how
people log in to the console. The normal login keeps its steps.
"""

from __future__ import annotations

import json
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
    _CLIENT_PROTOCOL_MAPPERS_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_authflow import get_admin_list_strict
from meho_backplane.connectors.keycloak.ops_authflow_touch_schemas import (
    AMR_MAPPER,
    ENSURE_APPROVAL_TOUCH_OP_ID,
    LOA_MAP_ATTRIBUTE,
    TOUCH_ACR,
    TOUCH_FLOW_ALIAS,
    TOUCH_FLOW_DESCRIPTION,
    TOUCH_FLOW_STEPS,
    TOUCH_LEVEL,
    FlowNode,
)
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import (
    KeycloakUnexpectedResponseError,
)
from meho_backplane.connectors.keycloak.ops_write_merge import merge_representation
from meho_backplane.connectors.keycloak.ops_write_preview import _managed_realm
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "KeycloakTouchFlowError",
    "keycloak_authflow_ensure_approval_touch",
    "wanted_steps",
]

_DEFAULT_CLIENT_ID = "meho-web"
#: A wanted step as Keycloak lists it: level, name, requirement, settings.
_Wanted = tuple[int, FlowNode]


class KeycloakTouchFlowError(RuntimeError):
    """The touch flow op stopped. ``code`` is a short, stable word.

    ``client_not_found``: the client does not exist. ``other_steps``: a flow
    with the name exists with another shape. ``loa_conflict``: the realm maps
    ``meho-touch`` to another level. ``check_failed``: something did not read
    back as written. ``flow_not_found``: the new flow cannot be found.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{ENSURE_APPROVAL_TOUCH_OP_ID} stopped ({code}): {message}")
        self.code = code


def wanted_steps(nodes: tuple[FlowNode, ...] = TOUCH_FLOW_STEPS, level: int = 0) -> list[_Wanted]:
    """The steps as Keycloak lists them: one flat list, a sub-flow's steps after it."""
    rows: list[_Wanted] = []
    for node in nodes:
        rows.append((level, node))
        if node.children is not None:
            rows.extend(wanted_steps(node.children, level + 1))
    return rows


def _name(row: dict[str, Any]) -> str:
    """A sub-flow is named by its alias (``displayName``), a step by its provider."""
    key = "displayName" if row.get("authenticationFlow") is True else "providerId"
    value = row.get(key)
    return value if isinstance(value, str) else ""


def _listed(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"level": row.get("level"), "step": _name(row), "requirement": row.get("requirement")}
        for row in rows
    ]


def _shape_difference(rows: list[dict[str, Any]], *, requirements: bool = True) -> str | None:
    """Name the first step that differs from the wanted one, or ``None``."""
    wanted = wanted_steps()
    for index, (level, node) in enumerate(wanted):
        if index >= len(rows):
            return f"step {index + 1} ({node.name!r}) is missing"
        row = rows[index]
        size = 3 if requirements else 2
        have: tuple[Any, ...] = (row.get("level"), _name(row), row.get("requirement"))[:size]
        want: tuple[Any, ...] = (level, node.name, node.requirement)[:size]
        if have != want:
            return f"step {index + 1} is {have!r}; wanted {want!r}"
    if len(rows) > len(wanted):
        return f"the flow has {len(rows)} steps; wanted {len(wanted)}"
    return None


async def _read_rows(
    connector: KeycloakConnector, operator: Operator, target: KeycloakTargetLike, realm: str
) -> list[dict[str, Any]]:
    path = fill_path(
        _AUTH_FLOW_EXECUTIONS_PATH, {"realm": realm, "flowAlias": quote_segment(TOUCH_FLOW_ALIAS)}
    )
    rows = await get_admin_list_strict(connector, target, path, operator=operator)
    for row in rows:
        if not isinstance(row, dict):
            raise KeycloakUnexpectedResponseError("a flow step is not a JSON object")
    return rows


async def _config_difference(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    rows: list[dict[str, Any]],
) -> str | None:
    """Name the first step whose settings differ from the wanted ones, or ``None``."""
    for index, (_, node) in enumerate(wanted_steps()):
        if node.config is None:
            continue
        config_id = rows[index].get("authenticationConfig")
        if not isinstance(config_id, str) or not config_id:
            return f"step {index + 1} ({node.name!r}) has no settings"
        path = fill_path(_AUTH_CONFIG_PATH, {"realm": realm, "id": quote_segment(config_id)})
        body = await connector._get_admin_json(target, path, operator=operator)
        if body.get("config") != node.config[1]:
            return f"step {index + 1} ({node.name!r}) has other settings"
    return None


async def _find_flow_id(
    connector: KeycloakConnector, operator: Operator, target: KeycloakTargetLike, realm: str
) -> str | None:
    path = fill_path(_AUTH_FLOWS_PATH, {"realm": realm})
    for flow in await get_admin_list_strict(connector, target, path, operator=operator):
        if isinstance(flow, dict) and flow.get("alias") == TOUCH_FLOW_ALIAS:
            flow_id = flow.get("id")
            if isinstance(flow_id, str) and flow_id:
                return flow_id
    return None


async def _add_steps(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    parent: str,
    nodes: tuple[FlowNode, ...],
) -> None:
    """Add *nodes* to the flow *parent*, each sub-flow with its own steps."""
    for node in nodes:
        values = {"realm": realm, "flowAlias": quote_segment(parent)}
        if node.children is None:
            path = fill_path(_AUTH_FLOW_ADD_STEP_PATH, values)
            body: dict[str, Any] = {"provider": node.name}
        else:
            path = fill_path(_AUTH_FLOW_ADD_SUBFLOW_PATH, values)
            body = {"alias": node.name, "type": "basic-flow", "description": node.description}
        await connector._write_admin(
            target, "POST", path, operator=operator, json=body, idempotent_conflict=False
        )
        if node.children is not None:
            await _add_steps(connector, operator, target, realm, node.name, node.children)


async def _make_flow(
    connector: KeycloakConnector, operator: Operator, target: KeycloakTargetLike, realm: str
) -> str:
    """Make the flow, its steps, their requirements and settings; return the flow id."""
    await connector._write_admin(
        target,
        "POST",
        fill_path(_AUTH_FLOWS_PATH, {"realm": realm}),
        operator=operator,
        json={
            "alias": TOUCH_FLOW_ALIAS,
            "description": TOUCH_FLOW_DESCRIPTION,
            "providerId": "basic-flow",
            "topLevel": True,
            "builtIn": False,
        },
        idempotent_conflict=False,
    )
    await _add_steps(connector, operator, target, realm, TOUCH_FLOW_ALIAS, TOUCH_FLOW_STEPS)
    rows = await _read_rows(connector, operator, target, realm)
    difference = _shape_difference(rows, requirements=False)
    if difference is not None:
        raise KeycloakTouchFlowError(
            "check_failed",
            f"flow {TOUCH_FLOW_ALIAS!r} was made, but its steps read back wrong: {difference}. "
            "No client was bound to it.",
        )
    executions_path = fill_path(
        _AUTH_FLOW_EXECUTIONS_PATH, {"realm": realm, "flowAlias": quote_segment(TOUCH_FLOW_ALIAS)}
    )
    # Keycloak makes every new step DISABLED. Steps are matched by place, not
    # by name: the level condition is in the flow twice.
    for row, (_, node) in zip(rows, wanted_steps(), strict=True):
        if row.get("requirement") != node.requirement:
            await connector._write_admin(
                target,
                "PUT",
                executions_path,
                operator=operator,
                json={**row, "requirement": node.requirement},
                idempotent_conflict=False,
            )
        if node.config is not None:
            alias, config = node.config
            path = fill_path(
                _AUTH_EXECUTION_CONFIG_PATH,
                {"realm": realm, "executionId": quote_segment(str(row["id"]))},
            )
            await connector._write_admin(
                target,
                "POST",
                path,
                operator=operator,
                json={"alias": alias, "config": dict(config)},
                idempotent_conflict=False,
            )
    flow_id = await _find_flow_id(connector, operator, target, realm)
    if flow_id is None:
        raise KeycloakTouchFlowError(
            "flow_not_found", f"flow {TOUCH_FLOW_ALIAS!r} was made but cannot be found"
        )
    return flow_id


def _loa_map(realm_rep: dict[str, Any]) -> dict[str, Any]:
    attributes = realm_rep.get("attributes")
    raw = attributes.get(LOA_MAP_ATTRIBUTE) if isinstance(attributes, dict) else None
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise KeycloakTouchFlowError(
            "loa_conflict", f"the realm attribute {LOA_MAP_ATTRIBUTE!r} is not a JSON map"
        ) from None
    if not isinstance(value, dict):
        raise KeycloakTouchFlowError(
            "loa_conflict", f"the realm attribute {LOA_MAP_ATTRIBUTE!r} is not a JSON map"
        )
    return value


async def _ensure_loa_map(
    connector: KeycloakConnector, operator: Operator, target: KeycloakTargetLike, realm: str
) -> bool:
    """Map ``meho-touch`` to level 2 in the realm; whether this run changed it."""
    realm_path = fill_path(_ADMIN_REALM_PATH, {"realm": realm})
    before = await connector._get_admin_json(target, realm_path, operator=operator)
    mapping = _loa_map(before)
    if TOUCH_ACR in mapping:
        if str(mapping[TOUCH_ACR]) != str(TOUCH_LEVEL):
            raise KeycloakTouchFlowError(
                "loa_conflict",
                f"the realm maps {TOUCH_ACR!r} to level {mapping[TOUCH_ACR]!r}, not "
                f"{TOUCH_LEVEL}. The flow was not bound. Fix the realm's ACR to LoA mapping.",
            )
        return False
    text = json.dumps({**mapping, TOUCH_ACR: TOUCH_LEVEL}, separators=(",", ":"))
    await connector._write_admin(
        target,
        "PUT",
        realm_path,
        operator=operator,
        json=merge_representation(before, {"attributes": {LOA_MAP_ATTRIBUTE: text}}),
        idempotent_conflict=False,
    )
    after = await connector._get_admin_json(target, realm_path, operator=operator)
    if str(_loa_map(after).get(TOUCH_ACR)) != str(TOUCH_LEVEL):
        raise KeycloakTouchFlowError(
            "check_failed", f"the realm does not read back with {TOUCH_ACR!r} at level 2"
        )
    return True


async def _ensure_amr_mapper(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    client_uuid: str,
) -> bool:
    """Add the ``amr`` mapper to the client unless it has one; whether it was added."""
    path = fill_path(
        _CLIENT_PROTOCOL_MAPPERS_PATH,
        {"realm": realm, "client-uuid": quote_segment(client_uuid)},
    )
    mappers = await get_admin_list_strict(connector, target, path, operator=operator)
    if any(
        isinstance(m, dict) and m.get("protocolMapper") == AMR_MAPPER["protocolMapper"]
        for m in mappers
    ):
        return False
    await connector._write_admin(
        target, "POST", path, operator=operator, json=dict(AMR_MAPPER), idempotent_conflict=False
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
        raise KeycloakTouchFlowError(
            "check_failed",
            f"the client's browser flow reads back as {read_back!r}, not the flow {flow_id!r}",
        )
    return {
        "browser_flow_id": read_back,
        "previous_browser_flow_id": previous,
        "binding_changed": changed,
    }


async def _ensure_flow(
    connector: KeycloakConnector, operator: Operator, target: KeycloakTargetLike, realm: str
) -> tuple[str, bool, list[dict[str, Any]]]:
    """Find or make the flow; return ``(flow_id, created, rows)``."""
    flow_id = await _find_flow_id(connector, operator, target, realm)
    created = flow_id is None
    if flow_id is None:
        flow_id = await _make_flow(connector, operator, target, realm)
    rows = await _read_rows(connector, operator, target, realm)
    difference = _shape_difference(rows) or await _config_difference(
        connector, operator, target, realm, rows
    )
    if difference is not None and created:
        raise KeycloakTouchFlowError(
            "check_failed",
            f"flow {TOUCH_FLOW_ALIAS!r} was made, but it reads back wrong: {difference}. "
            "No client was bound to it.",
        )
    if difference is not None:
        raise KeycloakTouchFlowError(
            "other_steps",
            f"flow {TOUCH_FLOW_ALIAS!r} exists with another shape: {difference}. Nothing "
            "was changed. Fix the flow by hand, or delete it and run again.",
        )
    return flow_id, created, rows


async def keycloak_authflow_ensure_approval_touch(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Give the console client the login flow with the passkey touch level."""
    realm = resolve_realm_config(target).managed_realm
    segment = quote_segment(realm)
    client_id = str(params.get("client_id") or _DEFAULT_CLIENT_ID).strip()
    client_uuid = await self._find_client_uuid(target, segment, client_id, operator=operator)
    if client_uuid is None:
        raise KeycloakTouchFlowError(
            "client_not_found", f"no client {client_id!r} in realm {realm!r}; nothing was changed"
        )
    flow_id, created, rows = await _ensure_flow(self, operator, target, segment)
    loa_changed = await _ensure_loa_map(self, operator, target, segment)
    mapper_added = await _ensure_amr_mapper(self, operator, target, segment, client_uuid)
    client_path = fill_path(
        _CLIENT_PATH, {"realm": segment, "client-uuid": quote_segment(client_uuid)}
    )
    binding = await _bind(self, operator, target, client_path, flow_id)
    return {
        "realm": realm,
        "flow_alias": TOUCH_FLOW_ALIAS,
        "flow_id": flow_id,
        "flow_created": created,
        "flow_steps": _listed(rows),
        "loa_map_changed": loa_changed,
        "amr_mapper_added": mapper_added,
        "client_id": client_id,
        "client_uuid": client_uuid,
        **binding,
    }


async def _touch_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Card for the op: the realm, the client, the new flow, who is affected."""
    client_id = str(ctx.params.get("client_id") or _DEFAULT_CLIENT_ID)
    return {
        "resource": "client_browser_flow",
        "realm": _managed_realm(ctx),
        "client_id": client_id,
        "new_flow": TOUCH_FLOW_ALIAS,
        "affects": (
            f"Every login to client {client_id!r} uses the new flow. The normal login "
            f"keeps its steps; a login that asks for {TOUCH_ACR!r} needs a passkey touch."
        ),
        "way_back": (
            'keycloak.client.update with {"authenticationFlowBindingOverrides": {"browser": ""}}'
        ),
    }


register_preview_builder(ENSURE_APPROVAL_TOUCH_OP_ID, _touch_preview)
