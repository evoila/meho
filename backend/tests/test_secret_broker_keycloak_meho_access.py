# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``secret.move`` into a password in MEHO's own login realm keeps MEHO's access.

Uses the mocked Keycloak of ``test_connectors_keycloak_meho_access_guard.py``
(made-up names and ids) and the in-process Vault fake as the source. What
is proven:

* a move into the password of the last active platform admin, of a login
  MEHO itself uses, or of the account that asks is refused before approval
  and when it runs; no password is sent;
* with a second platform admin, a move into the password of the first one
  waits for approval; the person who asked and an operator cannot approve
  it; another admin approves it, and then it runs.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx

import meho_backplane.connectors.secret  # noqa: F401 -- registers the keycloak + vault kinds
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.secret.ops import register_secret_broker_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations import dispatch
from meho_backplane.operations.approval_queue import (
    ApprovalRoleFloorError,
    RightsChangeApprovalError,
    approve_request,
)
from meho_backplane.settings import get_settings
from tests.test_connectors_keycloak_meho_access_guard import (  # noqa: F401  (fixtures)
    _ADMIN,
    _ADMIN_UUID,
    _KC_BASE_URL,
    _LOGIN_REALM,
    _LOGIN_REALM_TARGET,
    _PLATFORM_GROUP,
    _REQUESTER_UUID,
    _SECOND_ADMIN_UUID,
    _SERVICE_LOGIN,
    _TENANT_ID,
    _admin_client_env,
    _approve_and_run,
    _operator,
    _realm,
    _required_settings_env,
    _reset_module_state,
    _stored_requests,
    _user,
    kc,
)

from ._vault_fakes import install_fake_client

_SENTINEL = "made-up-moved-password-never-shown"
_MOVE = "secret.move"


@pytest.fixture(autouse=True)
def _vault_env(
    _admin_client_env: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    install_fake_client(monkeypatch, secret={"password": _SENTINEL})
    yield
    get_settings.cache_clear()


@pytest.fixture
async def broker(kc: KeycloakConnector) -> KeycloakConnector:  # noqa: F811
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_secret_broker_operations(embedding_service=service)
    return kc


def _move_to(username: str) -> dict[str, Any]:
    return {
        "from": f"vault:tenants/{_TENANT_ID}/db/prod#password",
        "to": f"keycloak:{_LOGIN_REALM_TARGET}/{_LOGIN_REALM}/{username}#password",
        "reason": "rotate a password",
    }


def _passwords_sent(mock: respx.MockRouter) -> list[str]:
    return [
        call.request.url.path
        for call in mock.calls
        if call.request.method == "PUT" and call.request.url.path.endswith("/reset-password")
    ]


async def _move(username: str, *, approved: bool) -> dict[str, Any]:
    result = await dispatch(
        operator=_operator(),
        connector_id="secret-broker-1.x",
        op_id=_MOVE,
        target=None,
        params=_move_to(username),
        _approved=approved,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


#: The user, the words of the refusal before approval, and the code at the run.
_CASES = [
    (_ADMIN, "the last active platform admin", "last_platform_admin"),
    (_SERVICE_LOGIN, "a login that MEHO itself uses", "meho_service_login"),
    ("the-requester", "the account that asked for this write", "own_account"),
]
_CASE_IDS = ["last-platform-admin", "meho-service-login", "own-account"]


@pytest.mark.parametrize(("username", "words", "code"), _CASES, ids=_CASE_IDS)
async def test_a_move_into_a_guarded_password_is_refused_before_approval(
    broker: KeycloakConnector, username: str, words: str, code: str
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _realm(mock)
        refused = await _move(username, approved=False)
        sent = _passwords_sent(mock)
    assert refused["extras"]["error_code"] == "refused_before_approval", refused
    assert words in refused["error"], refused["error"]
    assert username not in refused["error"]
    assert await _stored_requests() == []
    assert sent == []
    assert _SENTINEL not in str(refused)


@pytest.mark.parametrize(("username", "words", "code"), _CASES, ids=_CASE_IDS)
async def test_a_move_into_a_guarded_password_is_refused_when_it_runs(
    broker: KeycloakConnector, username: str, words: str, code: str
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _realm(mock)
        result = await _move(username, approved=True)
        sent = _passwords_sent(mock)
    assert result["status"] != "ok", result
    assert result["extras"]["exception_class"] == "MehoAccessRefusedError", result
    assert sent == []
    assert _SENTINEL not in str(result)


async def test_with_a_second_platform_admin_another_person_approves_the_move(
    broker: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.add(_user(_SECOND_ADMIN_UUID, "second-platform-admin"), _PLATFORM_GROUP)
        waiting = await _move(_ADMIN, approved=False)
        assert waiting["status"] == "awaiting_approval", waiting
        request_id = uuid.UUID(waiting["extras"]["approval_request_id"])
        # The person who asked cannot approve it, and an operator cannot either.
        not_allowed = [
            (_operator(_REQUESTER_UUID), RightsChangeApprovalError),
            (_operator("made-up-operator", TenantRole.OPERATOR), ApprovalRoleFloorError),
        ]
        for approver, error in not_allowed:
            async with get_sessionmaker()() as session:
                with pytest.raises(error):
                    await approve_request(session, request_id, operator=approver, params=None)
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            assert row.status == "pending"
        resumed = await _approve_and_run(waiting)
        sent = _passwords_sent(mock)
    assert resumed.status == "ok", resumed
    assert sent == [f"/admin/realms/{_LOGIN_REALM}/users/{_ADMIN_UUID}/reset-password"]
    assert _SENTINEL not in resumed.model_dump_json()
