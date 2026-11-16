# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every Keycloak user write uses the shared user check.

The test finds the Keycloak writes that take a user by their parameters: a
user id (``id`` or ``user_id``) and a ``username``. For each one it proves:

* the check before the wait for approval is the shared one
  (:func:`~meho_backplane.connectors.keycloak.ops_write_user_identity.refuse_other_user_check`);
* the run refuses an id and a username of different users, and writes nothing.

A new user write that forgets the check turns this test red.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

import pytest
import respx

import meho_backplane.connectors.keycloak as keycloak_package
import meho_backplane.operations.typed_register as typed_register
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.operations._park_check import _PARK_CHECKS
from tests.test_connectors_keycloak_user_identity import (  # noqa: F401  (fixtures)
    _GROUP_UUID,
    _KC_BASE_URL,
    _REALM,
    _USER_OP_PARAMS,
    _USER_UUID,
    _mount_token,
    _mount_user,
    _required_settings_env,
    _reset_module_state,
    _run,
    _user,
    kc,
)

# The checks register when their module is first imported. Import every module
# now, at collection: the per-test registry reset (conftest) would drop a check
# whose module an earlier test imported first.
for _module in pkgutil.iter_modules(keycloak_package.__path__):
    importlib.import_module(f"{keycloak_package.__name__}.{_module.name}")

_ACTIONS_PATH = f"/admin/realms/{_REALM}/authentication/required-actions"
_TOTP_ACTION = {"alias": "CONFIGURE_TOTP", "name": "Configure OTP", "enabled": True}

#: The other parameters each user write needs to reach the user check.
_OTHER_PARAMS: dict[str, dict[str, Any]] = {
    **_USER_OP_PARAMS,
    "keycloak.user.delete": {},
    "keycloak.user.required_actions.update": {"add": ["CONFIGURE_TOTP"]},
    "keycloak.group.member.add": {"group_id": _GROUP_UUID},
    "keycloak.group.member.remove": {"group_id": _GROUP_UUID},
}


async def _user_writes(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """op_id -> the parameter with the user id, for every write that takes a user."""
    found: dict[str, str] = {}

    async def _capture(**kwargs: Any) -> None:
        props = set((kwargs["parameter_schema"] or {}).get("properties", {}))
        id_keys = sorted(props & {"id", "user_id"})
        if kwargs["safety_level"] != "safe" and "username" in props and id_keys:
            found[kwargs["op_id"]] = "user_id" if "user_id" in id_keys else "id"

    monkeypatch.setattr(typed_register, "register_typed_operation", _capture)
    await KeycloakConnector.register_operations()
    return found


async def test_every_keycloak_user_write_uses_the_shared_user_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_writes = await _user_writes(monkeypatch)
    assert len(user_writes) >= 13, sorted(user_writes)
    for op_id in sorted(user_writes):
        check = _PARK_CHECKS.get(op_id)
        assert check is not None, f"{op_id} has no check before the wait for approval"
        assert check.__qualname__.startswith("refuse_other_user_check."), (
            f"{op_id} does not use refuse_other_user_check"
        )


async def test_every_keycloak_user_write_refuses_an_id_and_a_username_of_different_users(
    kc: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        user_writes = await _user_writes(patch)
    missing = sorted(set(user_writes) - set(_OTHER_PARAMS))
    assert not missing, f"add the other parameters of {missing} to _OTHER_PARAMS"
    for op_id, id_key in sorted(user_writes.items()):
        params = {id_key: _USER_UUID, "username": "someone-else", **_OTHER_PARAMS[op_id]}
        with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
            _mount_token(mock)
            _mount_user(mock, _user(True))
            mock.get(_ACTIONS_PATH).respond(200, json=[_TOTP_ACTION])
            writes = mock.route(method__in=["PUT", "POST", "DELETE"]).respond(204)
            result = await _run(op_id, params)
        assert result["status"] != "ok", (op_id, result)
        # Some ops withhold the error text, so the check falls back to the error class.
        assert "different users" in str(result) or "ValueError" in str(result), (op_id, result)
        assert not writes.called, op_id
