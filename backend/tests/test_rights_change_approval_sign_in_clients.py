# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The clients of the approval sign-in: update, create and delete.

In MEHO's own login realm these writes can change the sign-in MEHO asks
for before an approval:

* an update of, or a protocol mapper on, the client MEHO uses for the
  Keycloak admin API (``KEYCLOAK_ADMIN_CLIENT_ID``);
* a delete of a sign-in client, a new client with the ``clientId`` of one,
  and an update that sets such a ``clientId`` (compared without letter
  case).

A platform admin with a fresh sign-in in the web console does not run
them alone: they wait. The login that asked never approves them; a
second platform admin does. A create and a delete of another client keep
their rules. Fixture values are made up.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations._preview import PreviewContext, build_proposed_effect
from meho_backplane.operations._validate import compute_params_hash, policy_gate
from meho_backplane.operations.approval_queue import (
    ApprovalRoleFloorError,
    approve_request,
    create_pending_request,
)
from meho_backplane.operations.approval_rights import approve_right
from meho_backplane.operations.approval_sign_in_writes import approval_sign_in_check
from meho_backplane.settings import get_settings
from tests.test_keycloak_login_realm_every_write import _ctx, _FakeKeycloak, _Write
from tests.test_rights_change_approval_sign_in_writes import _CLIENT_UUID, _SIGN_IN_TEXT
from tests.test_rights_change_console_run import (
    _CONNECTOR_ID,
    _REALM,
    _TENANT_ID,
    _required_settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
    keycloak,  # noqa: F401 -- fixture: registers the Keycloak operations
)
from tests.test_rights_change_login_realm_platform_admin_alone import _login

_ADMIN_CLIENT = "meho-admin-api"


@pytest.fixture(autouse=True)
def _admin_client_env(
    _required_settings_env: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", _ADMIN_CLIENT)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _StoredClientKeycloak(_FakeKeycloak):
    """The fake Keycloak, which stores one client with the clientId *client_id*."""

    def __init__(self, client_id: str) -> None:
        super().__init__()
        self.client_id = client_id

    def connector(self) -> KeycloakConnector:
        connector = super().connector()
        read = connector._get_admin_json
        read_list = connector._get_admin_list
        client = {"id": _CLIENT_UUID, "clientId": self.client_id, "enabled": True}

        async def _get_admin_json(
            target: Any, path: str, *, operator: Any, params: Any = None
        ) -> dict[str, Any]:
            head, _sep, _client_uuid = path.rpartition("/")
            if head.endswith("/clients"):
                return dict(client)
            answer: dict[str, Any] = await read(target, path, operator=operator, params=params)
            return answer

        async def _get_admin_list(
            target: Any, path: str, *, operator: Any, params: Any = None, strict: bool = False
        ) -> list[Any]:
            if path.endswith("/clients") and (params or {}).get("clientId") == client["clientId"]:
                return [dict(client)]
            rows: list[Any] = await read_list(
                target, path, operator=operator, params=params, strict=strict
            )
            return rows

        connector._get_admin_json = _get_admin_json  # type: ignore[method-assign]
        connector._get_admin_list = _get_admin_list  # type: ignore[method-assign]
        return connector


def _mapper() -> dict[str, Any]:
    return {
        "name": "a-claim",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-hardcoded-claim-mapper",
        "config": {"claim.name": "team", "claim.value": "x"},
    }


#: (write, the clientId Keycloak stores for the client it names)
_WRITES: list[tuple[_Write, str]] = [
    (
        _Write(
            "keycloak.client.update",
            {
                "client_id": _ADMIN_CLIENT,
                "representation": {"redirectUris": ["https://x.example/*"]},
            },
        ),
        _ADMIN_CLIENT,
    ),
    (
        _Write(
            "keycloak.client.update",
            {"id": _CLIENT_UUID, "representation": {"serviceAccountsEnabled": True}},
        ),
        "MEHO-Admin-API",
    ),
    (
        _Write(
            "keycloak.protocol_mapper.create",
            {"client_id": _ADMIN_CLIENT, "representation": _mapper()},
        ),
        _ADMIN_CLIENT,
    ),
    (
        _Write(
            "keycloak.client.update",
            {"id": _CLIENT_UUID, "representation": {"clientId": "MEHO-Web-Approve"}},
        ),
        "example-app",
    ),
    (
        _Write("keycloak.client.create", {"representation": {"clientId": "Meho-Cli-Approve"}}),
        "example-app",
    ),
    (
        _Write("keycloak.client.create", {"representation": {"clientId": _ADMIN_CLIENT}}),
        "example-app",
    ),
    (_Write("keycloak.client.delete", {"client_id": "meho-web-approve"}), "meho-web-approve"),
    (_Write("keycloak.client.delete", {"id": _CLIENT_UUID}), "MEHO-CLI-APPROVE"),
    (_Write("keycloak.client.delete", {"id": _CLIENT_UUID}), _ADMIN_CLIENT),
]
_IDS = [
    "update-admin-client",
    "update-admin-client-by-id",
    "mapper-on-admin-client",
    "update-sets-approval-client-id",
    "create-approval-client",
    "create-admin-client",
    "delete-approval-client",
    "delete-approval-client-by-id",
    "delete-admin-client-by-id",
]

#: A create and a delete of another client.
_OTHER_CLIENT_WRITES: list[tuple[_Write, str]] = [
    (_Write("keycloak.client.create", {"representation": {"clientId": "new-app"}}), "x"),
    (_Write("keycloak.client.delete", {"client_id": "old-app"}), "old-app"),
]


async def _card(write: _Write, client_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    fake = _StoredClientKeycloak(client_id)
    params = copy.deepcopy(write.params)
    ctx: PreviewContext = _ctx(write.op_id, params, realm=_REALM, connector=fake.connector())
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    assert effect["preview"]["grant"]["login_realm"] is True
    return effect, params


async def _gate(monkeypatch: pytest.MonkeyPatch, write: _Write, client_id: str) -> str:
    effect, params = await _card(write, client_id)
    monkeypatch.setattr(
        "meho_backplane.operations.approval_object_check.live_card",
        AsyncMock(return_value=effect),
    )
    descriptor = await lookup_descriptor(
        tenant_id=_TENANT_ID,
        product="keycloak",
        version="26.x",
        impl_id="keycloak-admin",
        op_id=write.op_id,
    )
    assert descriptor is not None
    verdict, _reason = await policy_gate(
        operator=_login(),
        descriptor=descriptor,
        target=None,
        connector_id=_CONNECTOR_ID,
        params=params,
    )
    return verdict.value


async def _park(
    monkeypatch: pytest.MonkeyPatch,
    write: _Write,
    client_id: str,
    people_behind: list[dict[str, Any]] | None = None,
) -> uuid.UUID:
    effect, params = await _card(write, client_id)
    monkeypatch.setattr(
        "meho_backplane.operations.approval_object_check.live_card",
        AsyncMock(return_value=effect),
    )
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_login(),
            connector_id=_CONNECTOR_ID,
            op_id=write.op_id,
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
            proposed_effect=effect,
        )
        if people_behind is not None:
            request.people_behind = people_behind
        await session.commit()
        return request.id


async def _row(request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row


async def _approve(request_id: uuid.UUID, sub: str) -> None:
    row = await _row(request_id)
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=_login(sub), params=row.params)
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(("write", "client_id"), _WRITES, ids=_IDS)
async def test_a_platform_admin_with_a_fresh_sign_in_does_not_run_it_alone(
    keycloak: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    write: _Write,
    client_id: str,
) -> None:
    assert await _gate(monkeypatch, write, client_id) == "needs-approval"


@pytest.mark.asyncio
@pytest.mark.parametrize(("write", "client_id"), _WRITES, ids=_IDS)
async def test_the_login_that_asked_never_approves_it_a_second_platform_admin_does(
    keycloak: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    write: _Write,
    client_id: str,
) -> None:
    request_id = await _park(monkeypatch, write, client_id)
    assert approval_sign_in_check(await _row(request_id)) is True

    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(request_id, "platform-admin-a")
    assert _SIGN_IN_TEXT in exc.value.reason_text
    assert approve_right(_login(), await _row(request_id)).may_approve is False
    assert (await _row(request_id)).status == ApprovalRequestStatus.PENDING.value

    await _approve(request_id, "platform-admin-b")
    assert (await _row(request_id)).status == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
@pytest.mark.parametrize(("write", "client_id"), _WRITES, ids=_IDS)
async def test_a_person_behind_the_agent_that_asked_never_approves_it(
    keycloak: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    write: _Write,
    client_id: str,
) -> None:
    behind = [{"sub": "platform-admin-c", "why": "registered"}]
    request_id = await _park(monkeypatch, write, client_id, people_behind=behind)

    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(request_id, "platform-admin-c")
    assert _SIGN_IN_TEXT in exc.value.reason_text
    assert (await _row(request_id)).status == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("write", "client_id"), _OTHER_CLIENT_WRITES, ids=["create-other", "delete-other"]
)
async def test_a_create_or_delete_of_another_client_keeps_its_rules(
    keycloak: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    write: _Write,
    client_id: str,
) -> None:
    request_id = await _park(monkeypatch, write, client_id)
    assert approval_sign_in_check(await _row(request_id)) is False

    await _approve(request_id, "platform-admin-a")
    assert (await _row(request_id)).status == ApprovalRequestStatus.APPROVED.value
