# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Skip the one-time code after a passkey login: ``keycloak.authflow.ensure_passkey_code_skip``.

Keycloak 26.4 and newer puts the step ``conditional-credential`` ("Condition -
credential") into the browser flow of a new realm. With the config
``credentials = webauthn-passwordless`` and ``included = false`` the step is
true only when no passkey was used, so a passkey login skips the one-time
code. An upgrade does not add the step to an older realm (Keycloak 26.8.1:
``DefaultAuthenticationFlows.browserFlow`` adds it only when it makes a new
flow; ``MigrateTo26_4_0`` does not touch flows).

Keycloak refuses a new step in a built-in flow. So the op copies the
built-in ``browser`` flow, adds the step to the copy, and binds the copy as
the realm's browser flow. What the op does, on the target's managed realm:

1. Read the realm's ``browserFlow`` and the steps of that flow. Find the
   conditional sub-flow that holds ``auth-otp-form`` and
   ``conditional-user-configured`` (Keycloak lists a flow's steps as one
   flat list, depth first, with a ``level`` per row).
2. The step is already there (``conditional-credential``, required, config
   as above): change nothing.
3. The bound flow is not the built-in ``browser`` flow, or it has no such
   sub-flow: stop and change nothing.
4. Copy ``browser`` as ``browser with passkeys`` (Keycloak names the copied
   sub-flows ``browser with passkeys <old name>``). A copy with that name
   from a run that stopped half-way is used when its other steps match
   ``browser``; otherwise the op stops and names the difference.
5. In the copy's sub-flow, add the step, set it to required, give it its
   config, and move it right after ``conditional-user-configured``. Read
   the steps again and check them.
6. Bind the copy: read the realm, put ``browserFlow`` on top, write the full
   realm back, and read it again.

The way back is ``keycloak.realm.update`` with ``{"browserFlow": "browser"}``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.keycloak._paths import (
    _ADMIN_REALM_PATH,
    _AUTH_CONFIG_PATH,
    _AUTH_EXECUTION_CONFIG_PATH,
    _AUTH_EXECUTION_RAISE_PRIORITY_PATH,
    _AUTH_FLOW_ADD_STEP_PATH,
    _AUTH_FLOW_COPY_PATH,
    _AUTH_FLOW_EXECUTIONS_PATH,
    _AUTH_FLOWS_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_authflow import get_admin_list_strict
from meho_backplane.connectors.keycloak.ops_authflow_passkey_schemas import (
    ENSURE_PASSKEY_CODE_SKIP_OP_ID,
)
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import (
    KeycloakUnexpectedResponseError,
)
from meho_backplane.connectors.keycloak.ops_write_merge import (
    merge_representation,
    unrequested_changes,
)
from meho_backplane.connectors.keycloak.ops_write_preview import _managed_realm
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "BUILT_IN_BROWSER_FLOW",
    "ENSURE_PASSKEY_CODE_SKIP_OP_ID",
    "PASSKEY_FLOW_ALIAS",
    "KeycloakPasskeyFlowError",
    "keycloak_authflow_ensure_passkey_code_skip",
]

BUILT_IN_BROWSER_FLOW = "browser"
PASSKEY_FLOW_ALIAS = "browser with passkeys"
_CONDITION = "conditional-credential"
_USER_CONFIGURED = "conditional-user-configured"
_OTP_FORM = "auth-otp-form"
_PASSKEY_CREDENTIAL = "webauthn-passwordless"
_CONDITION_CONFIG = {"credentials": _PASSKEY_CREDENTIAL, "included": "false"}
_CONDITION_CONFIG_ALIAS = f"{PASSKEY_FLOW_ALIAS} - skip code after passkey"
#: Keycloak joins several credential types in one config value with ``##``.
_CONFIG_DELIMITER = "##"


class KeycloakPasskeyFlowError(RuntimeError):
    """The passkey step op stopped. ``code`` is a short, stable word.

    ``custom_flow``: the realm's browser flow is not the built-in one.
    ``no_code_step``: the flow has no conditional sub-flow with the one-time
    code. ``other_steps``: the flow or the copy has another shape.
    ``check_failed``: the copy or the binding did not read back as written.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{ENSURE_PASSKEY_CODE_SKIP_OP_ID} stopped ({code}): {message}")
        self.code = code


def _is_subflow(row: dict[str, Any]) -> bool:
    return row.get("authenticationFlow") is True


def _name(row: dict[str, Any]) -> str:
    """A sub-flow is named by its alias (``displayName``), a step by its provider."""
    value = row.get("displayName") if _is_subflow(row) else row.get("providerId")
    return value if isinstance(value, str) else ""


def _rows(raw: list[Any]) -> list[dict[str, Any]]:
    for row in raw:
        if not isinstance(row, dict) or not isinstance(row.get("level"), int):
            raise KeycloakUnexpectedResponseError("a flow step is not a JSON object with a level")
    return list(raw)


def _children(rows: list[dict[str, Any]], parent: int) -> list[int]:
    """The indexes of the direct steps of the sub-flow at *parent*."""
    level = rows[parent]["level"]
    found = []
    for index in range(parent + 1, len(rows)):
        row_level = rows[index]["level"]
        if row_level <= level:
            break
        if row_level == level + 1:
            found.append(index)
    return found


def _code_subflow(rows: list[dict[str, Any]]) -> int | None:
    """The conditional sub-flow that holds the one-time code, or ``None``.

    More than one such sub-flow is a shape the op does not guess about.
    """
    found = []
    for index, row in enumerate(rows):
        if not _is_subflow(row) or row.get("requirement") != "CONDITIONAL":
            continue
        names = {_name(rows[child]) for child in _children(rows, index)}
        if {_OTP_FORM, _USER_CONFIGURED} <= names:
            found.append(index)
    if len(found) > 1:
        raise KeycloakPasskeyFlowError(
            "other_steps",
            f"the flow has {len(found)} conditional sub-flows with the one-time code; "
            "nothing was changed",
        )
    return found[0] if found else None


def _conditions(rows: list[dict[str, Any]], sub: int) -> list[int]:
    return [child for child in _children(rows, sub) if _name(rows[child]) == _CONDITION]


def _listed(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"level": row.get("level"), "step": _name(row), "requirement": row.get("requirement")}
        for row in rows
    ]


async def _read_steps(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    alias: str,
) -> list[dict[str, Any]]:
    path = fill_path(
        _AUTH_FLOW_EXECUTIONS_PATH, {"realm": realm, "flowAlias": quote_segment(alias)}
    )
    return _rows(await get_admin_list_strict(connector, target, path, operator=operator))


async def _config_matches(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    row: dict[str, Any],
) -> bool:
    """``True`` when the step's config is ``webauthn-passwordless`` with ``included`` off."""
    config_id = row.get("authenticationConfig")
    if not isinstance(config_id, str) or not config_id:
        return False
    path = fill_path(_AUTH_CONFIG_PATH, {"realm": realm, "id": quote_segment(config_id)})
    body = await connector._get_admin_json(target, path, operator=operator)
    config = body.get("config")
    if not isinstance(config, dict):
        return False
    credentials = str(config.get("credentials") or "").split(_CONFIG_DELIMITER)
    included = str(config.get("included") or "false").strip().lower()
    return set(credentials) == {_PASSKEY_CREDENTIAL} and included != "true"


async def _step_is_there(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    rows: list[dict[str, Any]],
    sub: int,
) -> bool:
    """``True`` when the sub-flow has the step, required, with the passkey config."""
    for index in _conditions(rows, sub):
        row = rows[index]
        if row.get("requirement") == "REQUIRED" and await _config_matches(
            connector, operator, target, realm, row
        ):
            return True
    return False


def _copy_difference(
    source: list[dict[str, Any]], copy: list[dict[str, Any]], copy_sub: int | None
) -> str | None:
    """Name how the copy differs from ``browser`` besides the step, or ``None``."""
    if copy_sub is None:
        return "it has no conditional sub-flow with the one-time code"
    conditions = _conditions(copy, copy_sub)
    if len(conditions) > 1:
        return f"its one-time code sub-flow has {len(conditions)} credential conditions"
    rest = [row for index, row in enumerate(copy) if index not in conditions]
    for index, want in enumerate(source):
        if index >= len(rest):
            return f"step {index + 1} ({_name(want)!r}) is missing"
        have = rest[index]
        want_name = _name(want)
        if _is_subflow(want):
            want_name = f"{PASSKEY_FLOW_ALIAS} {want_name}"
        if (have["level"], _name(have), have.get("requirement")) != (
            want["level"],
            want_name,
            want.get("requirement"),
        ):
            return (
                f"step {index + 1} is {_name(have)!r} ({have.get('requirement')}); "
                f"wanted {want_name!r} ({want.get('requirement')})"
            )
    if len(rest) > len(source):
        return f"it has {len(rest)} steps besides the condition; wanted {len(source)}"
    return None


async def _finish_copy(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    source: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Give the copy the step, in the right place. Returns ``(steps, step_added)``."""
    rows = await _read_steps(connector, operator, target, realm, PASSKEY_FLOW_ALIAS)
    sub = _code_subflow(rows)
    difference = _copy_difference(source, rows, sub)
    if difference is not None or sub is None:
        raise KeycloakPasskeyFlowError(
            "other_steps",
            f"flow {PASSKEY_FLOW_ALIAS!r} exists, but {difference}. Nothing more was "
            "changed and the realm still uses its old browser flow. Delete or rename "
            "that flow, and run again.",
        )
    step_added = not _conditions(rows, sub)
    if step_added:
        path = fill_path(
            _AUTH_FLOW_ADD_STEP_PATH,
            {"realm": realm, "flowAlias": quote_segment(_name(rows[sub]))},
        )
        await connector._write_admin(
            target,
            "POST",
            path,
            operator=operator,
            json={"provider": _CONDITION},
            idempotent_conflict=False,
        )
        rows = await _read_steps(connector, operator, target, realm, PASSKEY_FLOW_ALIAS)
        sub = _code_subflow(rows)
        if sub is None or len(_conditions(rows, sub)) != 1:
            raise KeycloakPasskeyFlowError(
                "check_failed", "the new condition step cannot be found after it was added"
            )
    row = rows[_conditions(rows, sub)[0]]
    has_config = bool(row.get("authenticationConfig"))
    if has_config and not await _config_matches(connector, operator, target, realm, row):
        raise KeycloakPasskeyFlowError(
            "other_steps",
            f"the condition step in flow {PASSKEY_FLOW_ALIAS!r} has other settings. Nothing "
            "more was changed and the realm still uses its old browser flow.",
        )
    await _set_up_condition(connector, operator, target, realm, row, has_config=has_config)
    await _move_after_user_configured(connector, operator, target, realm, rows, sub)
    return await _check_copy(connector, operator, target, realm), step_added


async def _set_up_condition(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    row: dict[str, Any],
    *,
    has_config: bool,
) -> None:
    """Make the condition step required and give it the passkey config."""
    # Keycloak makes the new step DISABLED; it must be required.
    if row.get("requirement") != "REQUIRED":
        path = fill_path(
            _AUTH_FLOW_EXECUTIONS_PATH,
            {"realm": realm, "flowAlias": quote_segment(PASSKEY_FLOW_ALIAS)},
        )
        await connector._write_admin(
            target,
            "PUT",
            path,
            operator=operator,
            json={**row, "requirement": "REQUIRED"},
            idempotent_conflict=False,
        )
    if not has_config:
        path = fill_path(
            _AUTH_EXECUTION_CONFIG_PATH,
            {"realm": realm, "executionId": quote_segment(str(row["id"]))},
        )
        await connector._write_admin(
            target,
            "POST",
            path,
            operator=operator,
            json={"alias": _CONDITION_CONFIG_ALIAS, "config": dict(_CONDITION_CONFIG)},
            idempotent_conflict=False,
        )


async def _move_after_user_configured(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    rows: list[dict[str, Any]],
    sub: int,
) -> None:
    """Raise the condition step until it is right after ``conditional-user-configured``."""
    children = _children(rows, sub)
    names = [_name(rows[child]) for child in children]
    position = names.index(_CONDITION)
    wanted = names.index(_USER_CONFIGURED) + 1
    path = fill_path(
        _AUTH_EXECUTION_RAISE_PRIORITY_PATH,
        {"realm": realm, "executionId": quote_segment(str(rows[children[position]]["id"]))},
    )
    for _ in range(position - wanted):
        await connector._write_admin(
            target, "POST", path, operator=operator, idempotent_conflict=False
        )


async def _check_copy(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
) -> list[dict[str, Any]]:
    """Read the copy again: the step must be required, configured, after the user check."""
    rows = await _read_steps(connector, operator, target, realm, PASSKEY_FLOW_ALIAS)
    sub = _code_subflow(rows)
    names = [_name(rows[child]) for child in _children(rows, sub)] if sub is not None else []
    in_place = _USER_CONFIGURED in names and names[names.index(_USER_CONFIGURED) + 1 :][:1] == [
        _CONDITION
    ]
    if (
        sub is None
        or not in_place
        or not await _step_is_there(connector, operator, target, realm, rows, sub)
    ):
        raise KeycloakPasskeyFlowError(
            "check_failed",
            f"flow {PASSKEY_FLOW_ALIAS!r} does not read back with the condition step right "
            "after 'conditional-user-configured'. The realm was not bound to it.",
        )
    return rows


async def _bind(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm_path: str,
    before: dict[str, Any],
) -> list[str]:
    """Bind the copy as the realm's browser flow. Returns the unrequested changes."""
    requested = {"browserFlow": PASSKEY_FLOW_ALIAS}
    await connector._write_admin(
        target,
        "PUT",
        realm_path,
        operator=operator,
        json=merge_representation(before, requested),
        idempotent_conflict=False,
    )
    after = await connector._get_admin_json(target, realm_path, operator=operator)
    if after.get("browserFlow") != PASSKEY_FLOW_ALIAS:
        raise KeycloakPasskeyFlowError(
            "check_failed",
            f"the realm's browser flow reads back as {after.get('browserFlow')!r}, not "
            f"{PASSKEY_FLOW_ALIAS!r}. Flow {PASSKEY_FLOW_ALIAS!r} has the step.",
        )
    return unrequested_changes(before, after, requested)


async def keycloak_authflow_ensure_passkey_code_skip(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Give the realm's browser flow the step that skips the code after a passkey."""
    del params  # the op takes no params; the schema refuses any
    realm = resolve_realm_config(target).managed_realm
    segment = quote_segment(realm)
    realm_path = fill_path(_ADMIN_REALM_PATH, {"realm": segment})
    before = await self._get_admin_json(target, realm_path, operator=operator)
    bound = before.get("browserFlow")
    if not isinstance(bound, str) or not bound:
        raise KeycloakUnexpectedResponseError("the realm has no browserFlow")
    rows = await _read_steps(self, operator, target, segment, bound)
    sub = _code_subflow(rows)
    answer: dict[str, Any] = {
        "realm": realm,
        "previous_browser_flow": bound,
        "browser_flow": bound,
        "changed": False,
        "flow_copied": False,
        "step_added": False,
        "binding_changed": False,
        "unrequested_changes": [],
    }
    if sub is not None and await _step_is_there(self, operator, target, segment, rows, sub):
        return {**answer, "flow_steps": _listed(rows)}
    flows = await _top_level_flows(self, operator, target, segment)
    built_in = any(flow.get("alias") == bound and flow.get("builtIn") is True for flow in flows)
    if bound != BUILT_IN_BROWSER_FLOW or not built_in:
        raise KeycloakPasskeyFlowError(
            "custom_flow",
            f"the realm's browser flow is {bound!r}, not the built-in "
            f"{BUILT_IN_BROWSER_FLOW!r} flow, and it has no passkey condition. Nothing was "
            "changed. Add the step to that flow by hand.",
        )
    if sub is None:
        raise KeycloakPasskeyFlowError(
            "no_code_step",
            f"flow {bound!r} has no conditional sub-flow with the one-time code "
            f"({_USER_CONFIGURED!r} and {_OTP_FORM!r}). Nothing was changed.",
        )
    if _conditions(rows, sub):
        raise KeycloakPasskeyFlowError(
            "other_steps",
            f"flow {bound!r} has a credential condition with other settings. Nothing was "
            "changed. Fix it by hand in the Keycloak admin console.",
        )
    copied = not any(flow.get("alias") == PASSKEY_FLOW_ALIAS for flow in flows)
    if copied:
        path = fill_path(
            _AUTH_FLOW_COPY_PATH, {"realm": segment, "flowAlias": quote_segment(bound)}
        )
        await self._write_admin(
            target,
            "POST",
            path,
            operator=operator,
            json={"newName": PASSKEY_FLOW_ALIAS},
            idempotent_conflict=False,
        )
    copy_rows, step_added = await _finish_copy(self, operator, target, segment, rows)
    unrequested = await _bind(self, operator, target, realm_path, before)
    return {
        **answer,
        "browser_flow": PASSKEY_FLOW_ALIAS,
        "changed": True,
        "flow_copied": copied,
        "step_added": step_added,
        "binding_changed": True,
        "unrequested_changes": unrequested,
        "flow_steps": _listed(copy_rows),
    }


async def _top_level_flows(
    connector: KeycloakConnector, operator: Operator, target: KeycloakTargetLike, realm: str
) -> list[dict[str, Any]]:
    path = fill_path(_AUTH_FLOWS_PATH, {"realm": realm})
    flows = await get_admin_list_strict(connector, target, path, operator=operator)
    return [flow for flow in flows if isinstance(flow, dict)]


async def _passkey_code_skip_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Card for the op: the realm, the flow it copies, the new flow, who is affected."""
    return {
        "resource": "realm_browser_flow",
        "realm": _managed_realm(ctx),
        "copies_flow": BUILT_IN_BROWSER_FLOW,
        "new_flow": PASSKEY_FLOW_ALIAS,
        "affects": (
            "Every browser login of the realm will use the new flow. After a passkey "
            "login it asks no one-time code; after a password login it still does."
        ),
        "way_back": 'keycloak.realm.update with {"browserFlow": "browser"}',
    }


register_preview_builder(ENSURE_PASSKEY_CODE_SKIP_OP_ID, _passkey_code_skip_preview)
